#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assistente de primeira execucao do Cnpjs-searcher.

Faz, na ordem, apenas o que falta:

1. descobre o mes mais recente (servidor ou dados locais);
2. baixa os Estabelecimentos*.zip e o Municipios.zip que faltam;
3. confere tamanho e abertura dos zips;
4. monta o banco SQLite (sem indices) e depois cria os indices;
5. abre a interface de consulta.

Se o banco do mes ja estiver completo, nao pergunta nada: so abre a interface.
Se houver download a fazer, pergunta antes e mostra uma estimativa de tempo.

Uso:
    python iniciar.py
    python iniciar.py --verificar   # so mostra o estado, sem abrir a GUI
"""

import argparse
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import traceback
import zipfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError:
    sys.exit("Tkinter nao encontrado. No Debian/Ubuntu: sudo apt install python3-tk")

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from cnpjs import baixar, carregar

DADOS = RAIZ / "dados"
CNAE = RAIZ / "CNAE.CNAECSV"

# Estimativas conservadoras, medidas no servidor da Receita e neste projeto.
MBPS = 1.4                    # ~1,4 MB/s por conexao
MIN_CARGA_POR_GB = 2.4        # carga sem indices
MIN_INDICES_POR_GB = 2.4      # criacao dos indices
MARGEM = 1.25


# ------------------------------------------------------------------ formato
def fmt_bytes(valor):
    if valor >= 1e9:
        return f"{valor / 1e9:.1f} GB".replace(".", ",")
    if valor >= 1e6:
        return f"{valor / 1e6:.0f} MB"
    if valor >= 1e3:
        return f"{valor / 1e3:.0f} KB"
    return f"{valor} B"


def fmt_tempo(minutos):
    minutos = max(0.0, minutos)
    if minutos < 1:
        return "menos de 1 minuto"
    if minutos < 60:
        return f"~{minutos:.0f} min"
    horas = int(minutos // 60)
    resto = int(round(minutos % 60))
    if resto == 60:
        horas += 1
        resto = 0
    return f"~{horas}h{resto:02d}"


# ------------------------------------------------------------------- estado
def meses_locais():
    if not DADOS.exists():
        return []
    return sorted(
        pasta.name for pasta in DADOS.iterdir()
        if pasta.is_dir() and re.fullmatch(r"\d{4}-\d{2}", pasta.name)
    )


def ordem_arquivo(nome):
    numero = re.search(r"(\d+)$", Path(nome).stem)
    return int(numero.group(1)) if numero else 0


def filtrar_necessarios(itens):
    """Somente o que o banco/interface usam: Estabelecimentos + Municipios."""
    necessarios = [
        item for item in itens
        if item["nome"] == "Municipios.zip"
        or (item["nome"].startswith("Estabelecimentos") and item["nome"].endswith(".zip"))
    ]
    necessarios.sort(key=lambda item: (
        0 if item["nome"] == "Municipios.zip" else 1, ordem_arquivo(item["nome"])
    ))
    if not any(item["nome"].startswith("Estabelecimentos") for item in necessarios):
        raise RuntimeError("o mes nao tem nenhum Estabelecimentos*.zip")
    return necessarios


def itens_locais(pasta):
    if not pasta.exists():
        return []
    itens = []
    for caminho in pasta.iterdir():
        if not caminho.is_file():
            continue
        if caminho.name == "Municipios.zip" or (
                caminho.name.startswith("Estabelecimentos") and caminho.name.endswith(".zip")):
            itens.append({"nome": caminho.name, "tamanho": caminho.stat().st_size})
    return sorted(itens, key=lambda item: (
        0 if item["nome"] == "Municipios.zip" else 1, ordem_arquivo(item["nome"])
    ))


def arquivos_locais(pasta):
    return {item["nome"]: item["tamanho"] for item in itens_locais(pasta)}


def calcular_faltantes(pasta, itens):
    faltam, total = [], 0
    for item in itens:
        alvo = pasta / item["nome"]
        parte = alvo.with_name(alvo.name + ".part")
        if alvo.exists() and alvo.stat().st_size == item["tamanho"]:
            continue
        atual = parte.stat().st_size if parte.exists() else 0
        if atual > item["tamanho"]:
            atual = 0
        restante = max(0, item["tamanho"] - atual)
        faltam.append((item, restante))
        total += restante
    return faltam, total


def caminho_db(mes):
    return DADOS / f"cnpj-{mes}.db"


def caminho_estado(mes):
    return DADOS / f"cnpj-{mes}.json"


def ler_estado(mes):
    caminho = caminho_estado(mes)
    if not caminho.exists():
        return {}
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_estado(mes, arquivos, linhas, concluido=True, carregando=False):
    DADOS.mkdir(parents=True, exist_ok=True)
    caminho = caminho_estado(mes)
    temporario = caminho.with_suffix(".json.tmp")
    dados = {
        "mes": mes,
        "arquivos": arquivos,
        "linhas": linhas,
        "concluido": bool(concluido),
        "carregando": bool(carregando),
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
    }
    temporario.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(caminho)
    return dados


def banco_desatualizado(estado, arquivos, db_ok):
    """O banco precisa ser refeito?

    So os Estabelecimentos importam: adicionar/remover um shard muda os dados.
    Remover um zip local nao invalida um banco que ja tem os dados; adicionar
    um Municipios.zip e resolvido por `preparar`, sem recarregar tudo.
    """
    if not db_ok:
        return True
    if not estado:
        return False
    antigos = {nome: tamanho for nome, tamanho in (estado.get("arquivos") or {}).items()
               if nome.startswith("Estabelecimentos")}
    atuais = {nome: tamanho for nome, tamanho in arquivos.items()
              if nome.startswith("Estabelecimentos")}
    if atuais == antigos:
        return False
    if set(atuais) <= set(antigos) and all(
            atuais[nome] == antigos[nome] for nome in atuais):
        return False  # so remocao; o banco existente continua completo
    return True


def inspecionar_db(db, contar=True):
    """Tabelas/indices/linhas do banco, sem falhar se ele nao existir."""
    if not db.exists():
        return {"existe": False, "ok": False, "indices_faltando": [], "municipios": False}
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tabelas = {linha[0] for linha in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            indices = {linha[0] for linha in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            if "estabelecimentos" not in tabelas:
                return {"existe": True, "ok": False, "motivo": "sem tabela estabelecimentos",
                        "indices_faltando": [], "municipios": False}
            linhas = con.execute("SELECT count(*) FROM estabelecimentos").fetchone()[0] \
                if contar else None
        finally:
            con.close()
    except sqlite3.Error as erro:
        return {"existe": True, "ok": False, "motivo": str(erro),
                "indices_faltando": [], "municipios": False}

    faltando = [nome for nome, _sql in carregar.INDICES if nome not in indices]
    return {
        "existe": True,
        "ok": linhas is None or linhas > 0,
        "linhas": linhas,
        "indices_faltando": faltando,
        "municipios": "municipios" in tabelas,
    }


def estimar(faltam_bytes, precisa_carga, precisa_indices, zip_estabelecimentos):
    min_download = faltam_bytes / (MBPS * 1e6) / 60 * MARGEM
    min_carga = (zip_estabelecimentos / 1e9 * MIN_CARGA_POR_GB * MARGEM
                 if precisa_carga else 0.0)
    min_indices = (zip_estabelecimentos / 1e9 * MIN_INDICES_POR_GB * MARGEM
                   if precisa_carga or precisa_indices else 0.0)
    return {
        "min_download": min_download,
        "min_carga": min_carga,
        "min_indices": min_indices,
        "min_total": min_download + min_carga + min_indices,
    }


def espaco_livre_gb():
    base = DADOS if DADOS.exists() else RAIZ
    return shutil.disk_usage(base).free / 1e9


def avaliar():
    """Estado atual: o que baixar, o que montar e quanto tempo deve levar."""
    mensagens = []
    remoto_ok = False
    mes_remoto = None
    try:
        mes_remoto = baixar.ultimo_mes()
        remoto_ok = True
    except Exception as erro:
        mensagens.append(f"Servidor indisponivel: {erro}")

    mes_local = meses_locais()[-1] if meses_locais() else None
    if not mes_remoto and not mes_local:
        raise RuntimeError(
            "Sem dados locais e sem acesso ao servidor. Verifique a internet e tente de novo.")

    mes = max(m for m in (mes_remoto, mes_local) if m)
    pasta = DADOS / mes
    itens_remotos = None
    if remoto_ok:
        try:
            itens_remotos = filtrar_necessarios(baixar.listar(mes))
        except Exception as erro:
            mensagens.append(f"Nao consegui listar {mes} no servidor: {erro}")

    if remoto_ok and mes == mes_remoto and itens_remotos:
        itens = itens_remotos
    else:
        itens = itens_locais(pasta)
        if not itens:
            raise RuntimeError(
                f"Nao ha Estabelecimentos*.zip em {pasta} e o servidor nao esta disponivel.")
        if remoto_ok:
            mensagens.append(f"Usando os dados locais de {mes}.")

    partes = list(pasta.glob("*.part")) if pasta.exists() else []
    if partes:
        mensagens.append(
            f"{len(partes)} download(s) incompleto(s) (.part); com internet eles sao retomados.")

    arquivos = arquivos_locais(pasta)
    faltam, faltam_bytes = calcular_faltantes(pasta, itens)
    faltam_estab = any(item["nome"].startswith("Estabelecimentos") for item, _ in faltam)
    falta_municipios = any(item["nome"] == "Municipios.zip" for item, _ in faltam)

    db = caminho_db(mes)
    estado = ler_estado(mes)
    carregando = bool(estado.get("carregando"))
    info = inspecionar_db(db, contar=(not estado or estado.get("linhas") is None))

    if carregando:
        mensagens.append(
            "A carga anterior foi interrompida; o banco sera recriado.")
    precisa_carga = carregando or banco_desatualizado(
        estado, arquivos, bool(info.get("ok")))

    precisa_indices = bool(info.get("indices_faltando")) or (
        not info.get("municipios") and (pasta / "Municipios.zip").exists())
    if precisa_carga:
        precisa_indices = True

    # Estimativa para o caso "baixar tudo e preparar"; se o usuario recusar
    # o download, o trabalho real e menor.
    carga_estimada = precisa_carga or faltam_estab
    indices_estimados = precisa_indices or faltam_estab or falta_municipios
    zip_estabelecimentos = sum(
        item["tamanho"] for item in itens
        if item["nome"].startswith("Estabelecimentos"))
    estimativa = estimar(faltam_bytes, carga_estimada, indices_estimados,
                         zip_estabelecimentos)

    livre = espaco_livre_gb()
    necessario = faltam_bytes / 1e9
    if carga_estimada:
        necessario += zip_estabelecimentos / 1e9 * 6.0
    elif indices_estimados:
        necessario += zip_estabelecimentos / 1e9 * 3.0
    if necessario > 0:
        necessario += 2.0
        mensagens.append(
            f"Espaco livre: {livre:.0f} GB; estimativa necessaria: {necessario:.0f} GB.")
        if livre < necessario:
            mensagens.append("ATENCAO: o espaco livre pode nao ser suficiente.")
    else:
        mensagens.append(f"Espaco livre: {livre:.0f} GB.")

    if not CNAE.exists():
        mensagens.append(
            "CNAE.CNAECSV ausente na raiz: a interface nao mostrara descricoes de CNAE.")
    if not (pasta / "Municipios.zip").exists() and not falta_municipios:
        mensagens.append(
            "Municipios.zip ausente: o filtro de municipio usara apenas o codigo.")

    pronto = not faltam and not precisa_carga and not precisa_indices

    # Registra o estado de um banco que ja existia (inclusive o banco completo
    # criado fora deste assistente), para as proximas execucoes serem rapidas.
    if info.get("ok") and (not estado or (pronto and not estado.get("concluido"))):
        linhas = info.get("linhas") or estado.get("linhas")
        arquivos_estado = estado.get("arquivos") or arquivos
        salvar_estado(mes, arquivos_estado, linhas, concluido=not precisa_indices)

    return {
        "mes": mes,
        "pasta": str(pasta),
        "db": str(db),
        "itens": itens,
        "faltam": faltam,
        "faltam_bytes": faltam_bytes,
        "faltam_estab": faltam_estab,
        "falta_municipios": falta_municipios,
        "precisa_carga": precisa_carga,
        "precisa_indices": precisa_indices,
        "pronto": pronto,
        "carregando": carregando,
        "info": info,
        "estado": estado,
        "arquivos": arquivos,
        "estimativa": estimativa,
        "mensagens": mensagens,
        "remoto": remoto_ok,
    }


# ------------------------------------------------------------------ trabalho
class SaidaFila:
    """Captura stdout dos modulos e manda para a fila da GUI."""

    def __init__(self, fila):
        self.fila = fila
        self.buffer = ""

    def write(self, texto):
        if "\r" in texto:
            progresso = texto.rsplit("\r", 1)[-1].strip()
            if progresso:
                self.fila.put(("status", progresso))
            return
        self.buffer += texto
        while "\n" in self.buffer:
            linha, self.buffer = self.buffer.split("\n", 1)
            if linha.strip():
                self.fila.put(("log", linha))

    def flush(self):
        if self.buffer.strip():
            self.fila.put(("log", self.buffer))
        self.buffer = ""


def verificar_zips(pasta, itens, remoto, fila):
    for item in itens:
        caminho = pasta / item["nome"]
        if not caminho.exists():
            if item["nome"] == "Municipios.zip":
                fila.put(("log", "Municipios.zip ausente; seguindo sem nomes de municipios."))
                continue
            raise RuntimeError(f"{item['nome']} nao foi baixado")
        if remoto and caminho.stat().st_size != item["tamanho"]:
            raise RuntimeError(
                f"{item['nome']}: tamanho {caminho.stat().st_size} "
                f"(esperado {item['tamanho']})")
        try:
            with zipfile.ZipFile(caminho) as zip_arquivo:
                membros = [nome for nome in zip_arquivo.namelist() if not nome.endswith("/")]
                if not membros:
                    raise RuntimeError(f"{item['nome']}: zip sem arquivo interno")
        except zipfile.BadZipFile as erro:
            raise RuntimeError(f"{item['nome']}: zip invalido ({erro})") from erro
        fila.put(("log", f"{item['nome']}: ok"))


def executar(estado, baixar_agora, fila):
    mes = estado["mes"]
    pasta = Path(estado["pasta"])
    db = Path(estado["db"])

    with redirect_stdout(SaidaFila(fila)):
        if baixar_agora and estado["faltam"]:
            total = len(estado["faltam"])
            for numero, (item, restante) in enumerate(estado["faltam"], 1):
                if restante <= 0 and (pasta / item["nome"]).exists():
                    continue
                fila.put(("status",
                          f"Baixando {item['nome']} ({numero}/{total})..."))
                baixar.baixar_arquivo(item["nome"], mes=mes, destino=str(DADOS))
        elif estado["faltam_bytes"]:
            fila.put(("log", "Download ignorado; usando somente os arquivos locais."))

        if baixar_agora:
            itens_verificar = estado["itens"]
        else:
            itens_verificar = itens_locais(pasta)
        if not any(item["nome"].startswith("Estabelecimentos")
                   for item in itens_verificar):
            raise RuntimeError(
                "Nao ha Estabelecimentos*.zip no disco. Baixe os dados para continuar.")

        fila.put(("status", "Conferindo os arquivos baixados..."))
        verificar_zips(pasta, itens_verificar, estado["remoto"], fila)

        arquivos = arquivos_locais(pasta)
        estado_db = ler_estado(mes)
        carregando = bool(estado_db.get("carregando"))
        info = inspecionar_db(
            db, contar=(not estado_db or estado_db.get("linhas") is None))

        precisa_carga = carregando or banco_desatualizado(
            estado_db, arquivos, bool(info.get("ok")))

        precisa_indices = bool(info.get("indices_faltando")) or (
            not info.get("municipios") and (pasta / "Municipios.zip").exists())
        if precisa_carga:
            precisa_indices = True

        if precisa_carga:
            backup = None
            if db.exists():
                backup = db.with_name(
                    f"{db.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
                db.rename(backup)
                fila.put(("log", f"Banco anterior movido para {backup.name}"))
            try:
                salvar_estado(mes, arquivos, None, concluido=False, carregando=True)
                fila.put(("status", "Carregando os estabelecimentos no SQLite..."))
                total = carregar.carregar(
                    pasta, db, recriar=False, criar_indices=False)
                salvar_estado(mes, arquivos, total, concluido=False)
                fila.put(("status", "Criando os indices (essa parte demora)..."))
                carregar.preparar(db, pasta)
                salvar_estado(mes, arquivos, total, concluido=True)
                linhas = total
            except BaseException:
                if backup and backup.exists():
                    if db.exists():
                        backup.unlink()
                    else:
                        backup.rename(db)
                        if estado_db:
                            salvar_estado(
                                mes, estado_db.get("arquivos", arquivos),
                                estado_db.get("linhas"),
                                concluido=estado_db.get("concluido", False),
                                carregando=estado_db.get("carregando", False))
                        else:
                            caminho_estado(mes).unlink(missing_ok=True)
                raise
            if backup and backup.exists():
                backup.unlink()
            for antigo in db.parent.glob(f"{db.name}.bak-*"):
                antigo.unlink(missing_ok=True)
        else:
            if precisa_indices:
                fila.put(("status", "Criando/completando os indices..."))
                carregar.preparar(db, pasta)
            linhas = estado_db.get("linhas")
            if linhas is None:
                linhas = inspecionar_db(db, contar=True).get("linhas")
            arquivos_estado = estado_db.get("arquivos") or arquivos
            salvar_estado(mes, arquivos_estado, linhas, concluido=True)

    fila.put(("status", "Tudo pronto."))
    if linhas is None:
        linhas = 0
    fila.put(("log", f"Banco pronto: {db} ({linhas:,} linhas)".replace(",", ".")))
    fila.put(("pronto", str(db)))


# ---------------------------------------------------------------------- GUI
def montar_texto(estado):
    """Texto da pergunta com a estimativa de tempo."""
    est = estado["estimativa"]
    linhas = [f"Mes: {estado['mes']}", ""]
    if estado["faltam_bytes"]:
        linhas.append(f"Faltam {fmt_bytes(estado['faltam_bytes'])} para baixar.")
    if est["min_download"]:
        linhas.append(f"Download estimado: {fmt_tempo(est['min_download'])}")
    if est["min_carga"]:
        linhas.append(f"Carga do banco: {fmt_tempo(est['min_carga'])}")
    if est["min_indices"]:
        linhas.append(f"Criacao dos indices: {fmt_tempo(est['min_indices'])}")
    if est["min_total"]:
        linhas.append(f"Total estimado: {fmt_tempo(est['min_total'])}")
    if estado["faltam_estab"]:
        linhas.append("")
        linhas.append("Ha um shard novo: o banco sera recriado depois do download.")
    if estado["falta_municipios"]:
        linhas.append("Falta o Municipios.zip: a tabela de municipios sera completada.")
    if estado["mensagens"]:
        linhas += [""] + estado["mensagens"]
    linhas += ["", "O tempo varia com a conexao e o disco. Pegue um cafe e deixe rodando."]
    return "\n".join(linhas)


class Aplicativo:
    def __init__(self, raiz):
        self.raiz = raiz
        self.fila = queue.Queue()
        self.trabalhando = False

        raiz.title("CNPJ - Preparar dados")
        raiz.geometry("920x620")
        raiz.minsize(760, 480)
        raiz.protocol("WM_DELETE_WINDOW", self.fechar)

        topo = ttk.Frame(raiz, padding=(14, 12, 14, 6))
        topo.pack(fill="x")
        ttk.Label(topo, text="Cnpjs-searcher",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        ttk.Label(
            topo,
            text="Baixa os dados da Receita, monta o banco SQLite e abre a consulta.",
            foreground="#555").pack(anchor="w", pady=(2, 0))

        self.var_status = tk.StringVar(value="Verificando dados locais e servidor...")
        status = ttk.Frame(raiz, padding=(14, 6))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.var_status).pack(side="left")
        self.progresso = ttk.Progressbar(status, mode="indeterminate", length=180)
        self.progresso.pack(side="right")

        quadro = ttk.Frame(raiz, padding=(14, 6))
        quadro.pack(fill="both", expand=True)
        self.log = ScrolledText(quadro, height=20, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

        rodape = ttk.Frame(raiz, padding=(14, 6, 14, 12))
        rodape.pack(fill="x")
        self.btn_verificar = ttk.Button(
            rodape, text="Verificar novamente", command=self.verificar)
        self.btn_verificar.pack(side="left")
        ttk.Button(rodape, text="Sair", command=self.fechar).pack(side="right")

        self.raiz.after(100, self.verificar)
        self.raiz.after(100, self.processar_fila)

    # ------------------------------------------------------------ fila/GUI
    def registrar(self, texto):
        self.log.configure(state="normal")
        self.log.insert("end", texto + "\n")
        linhas = int(self.log.index("end-1c").split(".")[0])
        if linhas > 2500:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def processar_fila(self):
        for _ in range(80):
            try:
                tipo, dado = self.fila.get_nowait()
            except queue.Empty:
                break
            if tipo == "log":
                self.registrar(dado)
            elif tipo == "status":
                self.var_status.set(dado)
            elif tipo == "estado":
                self.receber_estado(dado)
            elif tipo == "pronto":
                self.receber_pronto(dado)
            elif tipo == "erro":
                self.receber_erro(dado)
        self.raiz.after(100, self.processar_fila)

    # ------------------------------------------------------------- estados
    def verificar(self):
        if self.trabalhando:
            return
        self.trabalhando = True
        self.var_status.set("Verificando dados locais e servidor...")
        self.progresso.start(12)
        self.btn_verificar.config(state="disabled")
        threading.Thread(target=self._verificar_worker, daemon=True).start()

    def _verificar_worker(self):
        try:
            self.fila.put(("estado", avaliar()))
        except Exception as erro:
            self.fila.put(("erro", str(erro)))

    def receber_estado(self, estado):
        self.trabalhando = False
        self.progresso.stop()
        self.btn_verificar.config(state="normal")
        for mensagem in estado["mensagens"]:
            self.registrar(mensagem)
        if estado["pronto"]:
            self.btn_verificar.config(state="disabled")
            self.var_status.set("Tudo pronto. Abrindo a interface...")
            self.registrar(f"Mes: {estado['mes']}")
            self.registrar(f"Banco: {estado['db']}")
            self.raiz.after(700, lambda: self.abrir_interface(estado["db"]))
            return
        self.perguntar(estado)

    def perguntar(self, estado):
        texto = montar_texto(estado)
        if estado["faltam_bytes"]:
            resposta = messagebox.askyesnocancel(
                "Baixar dados",
                texto + "\n\nBaixar agora?\n\n"
                "Sim = baixar e preparar tudo\n"
                "Nao = usar somente o que ja esta no disco\n"
                "Cancelar = sair")
            if resposta is None:
                self.fechar()
                return
            if not resposta:
                if not any(item["nome"].startswith("Estabelecimentos")
                           for item in itens_locais(Path(estado["pasta"]))):
                    messagebox.showerror(
                        "Sem dados",
                        "Nao ha Estabelecimentos*.zip no disco. "
                        "Baixe os dados ou cancele.")
                    self.var_status.set("Nenhum dado local para usar.")
                    return
            self.iniciar_trabalho(estado, bool(resposta))
        else:
            if not messagebox.askyesno("Preparar banco", texto + "\n\nPreparar o banco agora?"):
                self.fechar()
                return
            self.iniciar_trabalho(estado, False)

    def iniciar_trabalho(self, estado, baixar_agora):
        self.trabalhando = True
        self.var_status.set("Trabalhando...")
        self.progresso.start(12)
        self.btn_verificar.config(state="disabled")
        self.registrar("")
        self.registrar("=== Iniciando ===")
        threading.Thread(
            target=self._executar_worker, args=(estado, baixar_agora),
            daemon=True).start()

    def _executar_worker(self, estado, baixar_agora):
        try:
            executar(estado, baixar_agora, self.fila)
        except BaseException as erro:
            self.fila.put(("log", traceback.format_exc()))
            self.fila.put(("erro", str(erro)))

    def receber_pronto(self, db):
        self.trabalhando = False
        self.progresso.stop()
        self.var_status.set("Tudo pronto. Abrindo a interface...")
        self.raiz.after(700, lambda: self.abrir_interface(db))

    def receber_erro(self, mensagem):
        self.trabalhando = False
        self.progresso.stop()
        self.btn_verificar.config(state="normal")
        self.var_status.set("Erro.")
        self.registrar(f"ERRO: {mensagem}")
        messagebox.showerror("Erro", mensagem)

    def abrir_interface(self, db):
        try:
            subprocess.Popen(
                [sys.executable, "-m", "cnpjs.interface", "--db", db],
                cwd=str(RAIZ))
        except OSError as erro:
            messagebox.showerror("Erro", f"Nao consegui abrir a interface: {erro}")
            self.btn_verificar.config(state="normal")
            return
        self.raiz.destroy()

    def fechar(self):
        if self.trabalhando:
            if not messagebox.askyesno(
                    "Sair",
                    "Um processo esta rodando. Sair agora?\n"
                    "O download pode ser retomado na proxima execucao."):
                return
        self.raiz.destroy()


# --------------------------------------------------------------------- main
def main():
    os.chdir(RAIZ)
    parser = argparse.ArgumentParser(
        description="Baixa, prepara o banco e abre a interface de consulta.")
    parser.add_argument(
        "--verificar", action="store_true",
        help="somente verifica o estado e imprime o resultado (sem GUI)")
    args = parser.parse_args()

    if args.verificar:
        try:
            estado = avaliar()
        except Exception as erro:
            print(f"erro: {erro}", file=sys.stderr)
            return 1
        resumo = {
            "mes": estado["mes"],
            "pronto": estado["pronto"],
            "db": estado["db"],
            "linhas": estado["info"].get("linhas") or estado["estado"].get("linhas"),
            "faltam_bytes": estado["faltam_bytes"],
            "faltam": [
                {"nome": item["nome"], "restante": restante}
                for item, restante in estado["faltam"]
            ],
            "precisa_carga": estado["precisa_carga"],
            "precisa_indices": estado["precisa_indices"],
            "estimativa_min": estado["estimativa"],
            "mensagens": estado["mensagens"],
        }
        print(json.dumps(resumo, ensure_ascii=False, indent=2))
        return 0

    raiz = tk.Tk()
    Aplicativo(raiz)
    raiz.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
