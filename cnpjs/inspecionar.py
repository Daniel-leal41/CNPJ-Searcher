#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mostra como sao os CSVs dentro dos zips da Receita, sem baixar tudo.

O site aceita Range, entao o script baixa so o comeco de cada .zip (256 KB),
descomprime o inicio do CSV em memoria e mostra: nome interno, encoding,
delimitador, numero de colunas e as primeiras linhas.

Uso:
    python -m cnpjs.inspecionar
    python -m cnpjs.inspecionar --arquivo Empresas1.zip --limite 2
    python -m cnpjs.inspecionar --local dados/2026-10
"""

import argparse
import csv
import io
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from . import baixar

BLOCO_INICIAL = 256 * 1024
BLOCO_MAXIMO = 4 * 1024 * 1024


# ------------------------------------------------------------- leitura do zip
def _decodificar(bruto):
    """ASCII puro vira 'ascii'; senao tenta utf-8 e cai para cp1252."""
    if all(byte < 0x80 for byte in bruto):
        return bruto.decode("ascii"), "ascii"
    for corte in range(4):
        try:
            return bruto[: len(bruto) - corte].decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    return bruto.decode("latin-1"), "latin-1"


def ler_zip_bytes(dados, limite=3):
    """Le as primeiras linhas do primeiro CSV de um zip (bytes parciais)."""
    if len(dados) < 30 or struct.unpack_from("<I", dados)[0] != 0x04034B50:
        raise ValueError("nao parece ser um zip")

    metodo = struct.unpack_from("<H", dados, 8)[0]
    tamanho_csv = struct.unpack_from("<I", dados, 22)[0]
    tam_nome, tam_extra = struct.unpack_from("<HH", dados, 26)
    nome = dados[30:30 + tam_nome].decode("cp437")
    inicio = 30 + tam_nome + tam_extra
    bruto = dados[inicio:]

    if metodo == 8:
        bruto = zlib.decompressobj(-15).decompress(bruto)
    elif metodo == 0:
        bruto = bruto[:tamanho_csv]
    else:
        raise ValueError(f"metodo de compressao nao suportado: {metodo}")

    if not bruto.strip():
        raise ValueError("CSV vazio")
    texto, codificacao = _decodificar(bruto)
    linhas = [linha for linha in texto.splitlines() if linha.strip()]
    if not linhas:
        raise ValueError("zip sem linhas (talvez precise de mais bytes)")

    delimitador = ";" if linhas[0].count(";") >= linhas[0].count(",") else ","
    amostra = list(csv.reader(linhas[:limite], delimiter=delimitador, quotechar='"'))
    return {
        "interno": nome,
        "codificacao": codificacao,
        "delimitador": delimitador,
        "tamanho_csv": tamanho_csv,
        "colunas": [len(linha) for linha in amostra],
        "linhas": amostra,
    }


def _baixar_inicio(item, quantidade):
    url = urljoin(baixar.HOST, item["href"])
    pedido = Request(url, headers={"Range": f"bytes=0-{quantidade - 1}",
                                   "User-Agent": "inspecionar-cnpj/1.0"})
    with urlopen(pedido, timeout=60) as resposta:
        return resposta.read(quantidade)


def inspecionar(item, limite=3):
    """Tenta 256 KB, 1 MB e 4 MB ate conseguir descomprimir as linhas."""
    tamanho = BLOCO_INICIAL
    while True:
        try:
            return ler_zip_bytes(_baixar_inicio(item, tamanho), limite)
        except ValueError:
            if tamanho >= BLOCO_MAXIMO:
                raise
            tamanho *= 4


def inspecionar_local(caminho_zip, limite=3):
    with open(caminho_zip, "rb") as arquivo:
        return ler_zip_bytes(arquivo.read(BLOCO_MAXIMO), limite)


# ------------------------------------------------------------------- saida
def _tamanho(n):
    for unidade in ("B", "KB", "MB", "GB"):
        if n < 1000 or unidade == "GB":
            return f"{n:.0f} {unidade}" if unidade == "B" else f"{n:.1f} {unidade}"
        n /= 1000


def mostrar(nome_zip, info, limite):
    print(f"\n{nome_zip}  ->  {info['interno']}  ({_tamanho(info['tamanho_csv'])} descompactado)")
    colunas = info["colunas"]
    resumo = f"{colunas[0]} colunas" if len(set(colunas)) == 1 else f"colunas irregulares: {colunas}"
    print(f"  {info['codificacao']} | delimitador {info['delimitador']!r} | {resumo}")
    for numero, linha in enumerate(info["linhas"], 1):
        texto = " | ".join(campo[:60] for campo in linha)
        print(f"  {numero}: {texto[:200]}")


def main():
    p = argparse.ArgumentParser(description="Inspeciona os CSVs dentro dos zips da Receita.")
    p.add_argument("--mes", default="latest", help="AAAA-MM ou 'latest' (padrao)")
    p.add_argument("--arquivo", help="inspeciona somente este zip")
    p.add_argument("--limite", type=int, default=3, help="linhas por arquivo (padrao: 3)")
    p.add_argument("--local", help="pasta com zips ja baixados (nao usa a rede)")
    args = p.parse_args()

    try:
        if args.local:
            zips = sorted(Path(args.local).glob("*.zip"))
            if not zips:
                sys.exit(f"nenhum .zip em {args.local}")
            for caminho in zips:
                mostrar(caminho.name, inspecionar_local(caminho, args.limite), args.limite)
            return

        mes = baixar.ultimo_mes() if args.mes == "latest" else args.mes
        itens = baixar.listar(mes)
        if args.arquivo:
            itens = [i for i in itens if i["nome"] == args.arquivo]
            if not itens:
                sys.exit(f"{args.arquivo} nao existe em {mes}")
        print(f"{mes}: inspecionando {len(itens)} arquivo(s)")
        for item in itens:
            try:
                mostrar(item["nome"], inspecionar(item, args.limite), args.limite)
            except (OSError, ValueError) as erro:
                print(f"\n{item['nome']}: erro: {erro}")
    except OSError as erro:
        sys.exit(f"erro ao acessar o compartilhamento: {erro}")


if __name__ == "__main__":
    main()
