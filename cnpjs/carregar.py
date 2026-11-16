#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Carrega os Estabelecimentos da Receita (CSV dentro dos zips) em SQLite.

Le direto do .zip (sem extrair), junta os shards Estabelecimentos0..9 em uma
unica tabela e cria um banco por mes: dados/cnpj-2026-10.db.

Uso:
    python -m cnpjs.carregar
    python -m cnpjs.carregar --pasta dados/2026-10 --recriar
"""

import argparse
import csv
import io
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

COLUNAS_CSV = 30
LOTE = 10_000

SQL_INSERT = """
INSERT INTO estabelecimentos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

SCHEMA = """
CREATE TABLE estabelecimentos (
    cnpj TEXT PRIMARY KEY,
    cnpj_basico TEXT NOT NULL,
    matriz_filial TEXT,
    nome_fantasia TEXT,
    situacao TEXT,
    data_situacao TEXT,
    motivo_situacao TEXT,
    nome_cidade_exterior TEXT,
    pais TEXT,
    data_inicio_atividade TEXT,
    cnae_principal TEXT,
    cnae_secundaria TEXT,
    tipo_logradouro TEXT,
    logradouro TEXT,
    numero TEXT,
    complemento TEXT,
    bairro TEXT,
    cep TEXT,
    uf TEXT,
    municipio TEXT,
    ddd1 TEXT,
    telefone1 TEXT,
    ddd2 TEXT,
    telefone2 TEXT,
    ddd3 TEXT,
    telefone3 TEXT,
    email TEXT,
    situacao_especial TEXT,
    data_situacao_especial TEXT
)
"""

# cnae+situacao atende "CNAE X" e "CNAE X e ativa"; uf+situacao atende
# "ativas de um estado"; situacao atende so "ativa"; nome com COLLATE NOCASE
# deixa o LIKE 'texto%' da interface usar o indice.
INDICES = [
    ("idx_estab_cnae", "CREATE INDEX idx_estab_cnae ON estabelecimentos(cnae_principal, situacao)"),
    ("idx_estab_situacao", "CREATE INDEX idx_estab_situacao ON estabelecimentos(situacao)"),
    ("idx_estab_uf_municipio_situacao",
     "CREATE INDEX idx_estab_uf_municipio_situacao ON estabelecimentos(uf, municipio, situacao)"),
    ("idx_estab_uf_situacao", "CREATE INDEX idx_estab_uf_situacao ON estabelecimentos(uf, situacao)"),
    ("idx_estab_basico", "CREATE INDEX idx_estab_basico ON estabelecimentos(cnpj_basico)"),
    ("idx_estab_nome",
     "CREATE INDEX idx_estab_nome ON estabelecimentos(nome_fantasia COLLATE NOCASE)"),
    # Indices parciais: "tem e-mail"/"tem telefone" nao varrem 31M linhas.
    # O de e-mail inclui o telefone para cobrir os dois filtros juntos.
    ("idx_estab_email_contato",
     "CREATE INDEX idx_estab_email_contato ON estabelecimentos("
     "situacao, email, COALESCE(telefone1, telefone2, telefone3)) "
     "WHERE email IS NOT NULL"),
    ("idx_estab_telefone",
     "CREATE INDEX idx_estab_telefone ON estabelecimentos("
     "situacao, COALESCE(telefone1, telefone2, telefone3)) "
     "WHERE COALESCE(telefone1, telefone2, telefone3) IS NOT NULL"),
    ("idx_estab_inicio_situacao",
     "CREATE INDEX idx_estab_inicio_situacao ON estabelecimentos("
     "data_inicio_atividade, situacao)"),
]

# Nomes de versoes anteriores, substituidos pelos indices acima. Dropar depois
# de criar os novos evita ficar sem indice se a migracao for interrompida.
INDICES_OBSOLETOS = (
    "DROP INDEX IF EXISTS idx_estab_uf",
    "DROP INDEX IF EXISTS idx_estab_email",
)

SCHEMA_MUNICIPIOS = """
CREATE TABLE municipios (
    codigo TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    uf TEXT
)
"""

PRAGMAS = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-262144;
"""


# ------------------------------------------------------------------ leitura
def _texto(campo):
    return campo.strip() or None


def _tupla(linha):
    """30 campos do CSV -> 29 colunas (CNPJ completo calculado)."""
    return (
        linha[0] + linha[1] + linha[2],  # cnpj
        linha[0],                        # cnpj_basico
        *(_texto(campo) for campo in linha[3:]),
    )


def _ordem(caminho):
    """Ordena Estabelecimentos2 antes de Estabelecimentos10."""
    numero = re.search(r"(\d+)$", caminho.stem)
    return (int(numero.group(1)) if numero else 0, caminho.name)


def _carregar_zip(con, caminho, pares=None):
    with zipfile.ZipFile(caminho) as z:
        membros = [nome for nome in z.namelist() if not nome.endswith("/")]
        if not membros:
            raise ValueError(f"{caminho.name}: zip sem arquivo")
        with z.open(membros[0]) as bruto:
            leitor = csv.reader(
                io.TextIOWrapper(bruto, encoding="latin-1", newline=""),
                delimiter=";", quotechar='"',
            )
            total, inicio, lote = 0, time.monotonic(), []
            for numero, linha in enumerate(leitor, 1):
                if len(linha) != COLUNAS_CSV:
                    raise ValueError(f"{caminho.name} linha {numero}: "
                                     f"{len(linha)} colunas (esperado {COLUNAS_CSV})")
                if pares is not None and linha[19] and linha[20]:
                    pares.add((linha[19], linha[20]))
                lote.append(_tupla(linha))
                if len(lote) >= LOTE:
                    con.executemany(SQL_INSERT, lote)
                    total += len(lote)
                    lote.clear()
                    if total % 1_000_000 < LOTE:
                        ritmo = total / (time.monotonic() - inicio)
                        print(f"  {total:,} linhas ({ritmo:,.0f}/s)".replace(",", "."))
            if lote:
                con.executemany(SQL_INSERT, lote)
                total += len(lote)
    return total


# --------------------------------------------------------------- municipios
def _existe_tabela(con, nome):
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone())


def _ler_municipios(pasta):
    """(codigo, nome) do Municipios.zip, se ele estiver na pasta."""
    caminho = Path(pasta) / "Municipios.zip"
    if not caminho.exists():
        return []
    with zipfile.ZipFile(caminho) as z:
        membros = [nome for nome in z.namelist() if not nome.endswith("/")]
        if not membros:
            return []
        with z.open(membros[0]) as bruto:
            leitor = csv.reader(
                io.TextIOWrapper(bruto, encoding="latin-1", newline=""),
                delimiter=";", quotechar='"',
            )
            return [(linha[0].strip(), linha[1].strip())
                    for linha in leitor
                    if len(linha) >= 2 and linha[0].strip() and linha[1].strip()]


def _completar_uf(con, pares):
    """Preenche municipios.uf a partir dos pares (uf, codigo) vistos nos dados."""
    if not pares:
        return
    con.executemany(
        "UPDATE municipios SET uf = ? WHERE codigo = ? AND uf IS NULL",
        sorted(pares),
    )


def _criar_municipios(con, pasta, pares):
    """Cria a tabela municipios (codigo, nome, uf) se ainda nao existir."""
    if _existe_tabela(con, "municipios"):
        _completar_uf(con, pares)
        return False
    nomes = _ler_municipios(pasta)
    if not nomes:
        return False
    con.execute(SCHEMA_MUNICIPIOS)
    con.executemany("INSERT INTO municipios(codigo, nome) VALUES (?, ?)", nomes)
    _completar_uf(con, pares)
    return True


def _pares_uf(con):
    """(uf, municipio) distintos, usando o indice (uf, municipio, situacao)."""
    return set(con.execute(
        "SELECT uf, municipio FROM estabelecimentos "
        "WHERE uf IS NOT NULL AND municipio IS NOT NULL GROUP BY uf, municipio"
    ))


# ------------------------------------------------------------------- carga
def carregar(pasta, db, recriar=False, criar_indices=True, arquivo=None):
    """Carrega Estabelecimentos*.zip de pasta no banco SQLite db."""
    pasta, db = Path(pasta), Path(db)
    zips = sorted(pasta.glob("Estabelecimentos*.zip"), key=_ordem)
    if arquivo:
        zips = [caminho for caminho in zips if caminho.name == arquivo]
    if not zips:
        raise FileNotFoundError(f"nenhum Estabelecimentos*.zip em {pasta}"
                                + (f" com o nome {arquivo}" if arquivo else ""))
    if db.exists():
        if not recriar:
            raise FileExistsError(f"{db} ja existe (use --recriar)")
        db.unlink()

    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(PRAGMAS)
        con.execute(SCHEMA)
        total, pares = 0, set()
        for caminho in zips:
            print(f"{caminho.name}:")
            linhas = _carregar_zip(con, caminho, pares)
            con.commit()
            total += linhas
            print(f"  ok: {linhas:,} linhas (total {total:,})".replace(",", "."))
        if criar_indices:
            print("criando indices...")
            for nome, sql in INDICES:
                con.execute(sql)
            con.commit()
            print("indices ok")
        if _criar_municipios(con, pasta, pares):
            print("tabela municipios ok")
        con.execute("ANALYZE")
        con.commit()
    except BaseException:
        con.close()
        db.unlink(missing_ok=True)
        raise
    con.close()
    return total


def preparar(db, pasta):
    """Completa um banco ja carregado: tabela municipios e indices que faltam.

    Nao apaga nada. E idempotente; pode ser rodado de novo apos uma carga
    interrompida. Use journal_mode=OFF para nao encher o disco com WAL.
    """
    db, pasta = Path(db), Path(pasta)
    if not db.exists():
        raise FileNotFoundError(f"{db} nao existe")
    con = sqlite3.connect(db)
    criados = []
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-524288")
        existentes = {linha[0] for linha in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for nome, sql in INDICES:
            if nome not in existentes:
                print(f"criando {nome}...")
                con.execute(sql)
                con.commit()
                criados.append(nome)
        antigos = [nome for nome in ("idx_estab_uf", "idx_estab_email")
                   if nome in existentes]
        if antigos:
            for sql in INDICES_OBSOLETOS:
                con.execute(sql)
            con.commit()
            criados.append("indices antigos removidos")
        if _existe_tabela(con, "municipios"):
            if con.execute("SELECT 1 FROM municipios WHERE uf IS NULL LIMIT 1").fetchone():
                _completar_uf(con, _pares_uf(con))
                criados.append("municipios (uf)")
        elif _criar_municipios(con, pasta, _pares_uf(con)):
            criados.append("municipios")
        con.execute("ANALYZE")
        con.commit()
    finally:
        con.close()
    return criados



def pasta_padrao():
    pastas = sorted(p for p in Path("dados").glob("20??-??") if p.is_dir())
    return pastas[-1] if pastas else Path("dados")


def main():
    p = argparse.ArgumentParser(description="Carrega Estabelecimentos (zips) em SQLite.")
    p.add_argument("--pasta", help="pasta com os Estabelecimentos*.zip (padrao: mes mais novo em dados/)")
    p.add_argument("--db", help="arquivo do banco (padrao: cnpj-<mes>.db na pasta acima)")
    p.add_argument("--recriar", action="store_true", help="apaga o banco existente")
    p.add_argument("--arquivo", help="carrega so este zip (ex.: Estabelecimentos0.zip)")
    p.add_argument("--sem-indices", action="store_true", help="nao cria indices (mais rapido)")
    p.add_argument("--preparar", action="store_true",
                   help="adiciona municipios e indices que faltam a um banco existente")
    args = p.parse_args()

    pasta = Path(args.pasta) if args.pasta else pasta_padrao()
    db = Path(args.db) if args.db else pasta.parent / f"cnpj-{pasta.name}.db"
    try:
        if args.preparar:
            criados = preparar(db, pasta)
            print("preparado: " + (", ".join(criados) if criados else "nada a fazer"))
            return
        total = carregar(pasta, db, args.recriar, not args.sem_indices, args.arquivo)
    except (OSError, ValueError) as erro:
        sys.exit(f"erro: {erro}")
    print(f"{total:,} estabelecimentos em {db}".replace(",", "."))


if __name__ == "__main__":
    main()
