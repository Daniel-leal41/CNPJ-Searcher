#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baixa os arquivos de CNPJ da Receita Federal (Nextcloud publico).

Tres funcionalidades:

1. sincronizar()      -> baixa todos os arquivos do mes mais recente;
2. sincronizar() de novo depois -> baixa so o que apareceu de novo
   (um arquivo novo, como Estabelecimentos10.zip, ou uma pasta/mes nova);
3. baixar_arquivo()   -> baixa um unico arquivo isoladamente.

Uso:
    python -m cnpjs.baixar
    python -m cnpjs.baixar --arquivo Estabelecimentos10.zip
"""

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request, urlopen

TOKEN = "gn672Ad4CF8N6TK"
HOST = "https://arquivos.receitafederal.gov.br"
BASE = f"{HOST}/public.php/dav/files/{TOKEN}"
RAIZ = "Dados/Cadastros/CNPJ"
DESTINO = "dados"
TENTATIVAS = 3
BLOCO = 1 << 20


# --------------------------------------------------------------- servidor
def _abrir(url, metodo="GET", cabecalhos=None):
    req = Request(url, method=metodo, headers={"User-Agent": "baixar-cnpj/1.0", **(cabecalhos or {})})
    return urlopen(req, timeout=60)


def _itens_dav(xml):
    itens = []
    for no in ET.fromstring(xml).findall("{DAV:}response"):
        href = unquote(no.findtext("{DAV:}href") or "")
        props = no.find("{DAV:}propstat/{DAV:}prop")
        if props is None:
            continue
        nome = href.rstrip("/").rsplit("/", 1)[-1]
        if nome:
            itens.append({
                "nome": nome,
                "tamanho": int(props.findtext("{DAV:}getcontentlength") or 0),
                "href": href,
                "pasta": props.find("{DAV:}resourcetype/{DAV:}collection") is not None,
            })
    return itens


def _listar_dav(caminho):
    """Itens de um diretorio: [{nome, tamanho, href, pasta}].

    O servidor as vezes devolve uma resposta vazia ou um XML invalido; nesse
    caso tenta de novo e, no fim, levanta um IOError com o nome do diretorio
    em vez de deixar vazar um ParseError.
    """
    url = f"{BASE}/{quote(caminho.strip('/'))}/"
    ultimo = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            with _abrir(url, "PROPFIND", {"Depth": "1"}) as resposta:
                xml = resposta.read()
            if not xml.strip():
                raise IOError("resposta vazia do servidor")
            return _itens_dav(xml)
        except HTTPError as erro:
            if erro.code < 500 and erro.code != 429:
                raise IOError(
                    f"o servidor respondeu {erro.code} ao listar {caminho}") from erro
            ultimo = erro
        except (OSError, ET.ParseError) as erro:
            ultimo = erro
        if tentativa < TENTATIVAS:
            print(f"listar {caminho}: {ultimo}; tentando de novo em {tentativa}s")
            time.sleep(tentativa)
    raise IOError(f"nao consegui listar {caminho} no servidor: {ultimo}")


def ultimo_mes():
    """Pasta de mes (AAAA-MM) mais recente no servidor."""
    meses = sorted(i["nome"] for i in _listar_dav(RAIZ) if i["pasta"] and "-" in i["nome"])
    if not meses:
        raise IOError("nenhuma pasta de mes encontrada no servidor")
    return meses[-1]


def listar(mes):
    """Arquivos (sem pastas) de um mes."""
    return [i for i in _listar_dav(f"{RAIZ}/{mes}") if not i["pasta"]]


# --------------------------------------------------------------- download
def _tamanho(n):
    return f"{n / 1000:.1f} KB" if n < 1_000_000 else f"{n / 1e6:.1f} MB"


def _baixar(item, pasta):
    alvo = Path(pasta) / item["nome"]
    if alvo.exists() and alvo.stat().st_size == item["tamanho"]:
        print(f"{item['nome']}: ja existe")
        return alvo

    Path(pasta).mkdir(parents=True, exist_ok=True)
    parte = alvo.with_name(alvo.name + ".part")
    feito = parte.stat().st_size if parte.exists() else 0
    if feito > item["tamanho"]:
        parte.unlink()
        feito = 0
    if feito:
        print(f"{item['nome']}: retomando de {_tamanho(feito)}")

    cabecalhos = {"Range": f"bytes={feito}-"} if feito else {}
    with _abrir(urljoin(HOST, item["href"]), "GET", cabecalhos) as resposta:
        if feito and resposta.status != 206:
            feito = 0
        with open(parte, "ab" if feito else "wb") as arquivo:
            while pedaco := resposta.read(BLOCO):
                arquivo.write(pedaco)
                feito += len(pedaco)
                total = item["tamanho"] or feito
                sys.stdout.write(f"\r{item['nome']}: {100 * feito / total:5.1f}% "
                                 f"({_tamanho(feito)} / {_tamanho(total)})")
                sys.stdout.flush()
    print()

    if parte.stat().st_size != item["tamanho"]:
        baixado = parte.stat().st_size
        parte.unlink(missing_ok=True)
        raise IOError(f"{item['nome']}: baixou {baixado} de {item['tamanho']} bytes")
    parte.replace(alvo)
    print(f"{item['nome']}: baixado")
    return alvo


def _baixar_com_retry(item, pasta):
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            return _baixar(item, pasta)
        except OSError as erro:
            if tentativa == TENTATIVAS:
                raise
            print(f"{item['nome']}: erro ({erro}); tentando de novo em {tentativa}s")
            time.sleep(tentativa)


def baixar_arquivo(nome, mes=None, destino=DESTINO):
    """Baixa um unico arquivo (funcionalidade 3). Retorna o caminho local."""
    mes = mes or ultimo_mes()
    item = next((i for i in listar(mes) if i["nome"] == nome), None)
    if item is None:
        raise ValueError(f"{nome} nao existe em {mes}")
    return _baixar_com_retry(item, Path(destino) / mes)


def baixar_item(item, mes=None, destino=DESTINO):
    """Baixa um item ja listado, sem repetir o PROPFIND da pasta."""
    mes = mes or ultimo_mes()
    return _baixar_com_retry(item, Path(destino) / mes)


def sincronizar(destino=DESTINO, mes=None):
    """Baixa o que falta no mes (funcionalidades 1 e 2).

    Tambem detecta pasta de mes nova (passa a usar a mais recente) e arquivo
    novo dentro do mes. Retorna (mes, [nomes baixados]).
    """
    mes = mes or ultimo_mes()
    pasta = Path(destino) / mes
    baixados = []
    for item in listar(mes):
        alvo = pasta / item["nome"]
        if alvo.exists() and alvo.stat().st_size == item["tamanho"]:
            continue
        _baixar_com_retry(item, pasta)
        baixados.append(item["nome"])
    return mes, baixados


# -------------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="Baixa os arquivos de CNPJ da Receita Federal.")
    p.add_argument("--arquivo", help="baixa somente este arquivo (ex.: Estabelecimentos10.zip)")
    p.add_argument("--mes", help="AAAA-MM (padrao: o mais recente)")
    p.add_argument("--destino", default=DESTINO, help="pasta de destino (padrao: dados)")
    args = p.parse_args()

    try:
        if args.arquivo:
            print(baixar_arquivo(args.arquivo, args.mes, args.destino))
        else:
            mes, baixados = sincronizar(args.destino, args.mes)
            print(f"{mes}: {len(baixados)} arquivo(s) novo(s)")
    except (OSError, ValueError) as erro:
        sys.exit(f"erro: {erro}")


if __name__ == "__main__":
    main()
