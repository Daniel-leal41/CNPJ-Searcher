#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do cnpjs.baixar contra um Nextcloud falso (sem baixar 7 GB)."""

import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, unquote

from cnpjs import baixar as app

TOKEN = "teste"
PREFIXO = f"/public.php/dav/files/{TOKEN}/Dados/Cadastros/CNPJ/"


class ServidorFalso(BaseHTTPRequestHandler):
    arquivos = {}         # "2026-10/Cnaes.zip" -> bytes
    pastas = set()        # {"2026-10"}
    requisicoes = []      # (metodo, caminho, Range)
    falhas_propfind = 0   # proximas N listagens voltam vazias

    def log_message(self, *args):
        pass

    def _xml(self, href, pasta, tamanho=0):
        tipo = "<d:collection/>" if pasta else ""
        tamanho = "" if pasta else f"<d:getcontentlength>{tamanho}</d:getcontentlength>"
        return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                f"<d:resourcetype>{tipo}</d:resourcetype>{tamanho}"
                f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    def do_PROPFIND(self):
        caminho = unquote(self.path)
        self.requisicoes.append(("PROPFIND", caminho, None))
        if ServidorFalso.falhas_propfind > 0:
            ServidorFalso.falhas_propfind -= 1
            self.send_response(207)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not caminho.startswith(PREFIXO):
            return self.send_error(404)
        resto = caminho[len(PREFIXO):].strip("/")
        corpo = f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'

        if not resto:  # lista as pastas de mes
            corpo += self._xml(PREFIXO, True)
            for mes in sorted(self.pastas):
                corpo += self._xml(PREFIXO + mes + "/", True)
        else:  # lista os arquivos do mes
            corpo += self._xml(PREFIXO + resto + "/", True)
            for nome, dados in self.arquivos.items():
                pasta, arquivo = nome.split("/", 1)
                if pasta == resto:
                    corpo += self._xml(PREFIXO + quote(nome), False, len(dados))
        corpo += "</d:multistatus>"

        dados = corpo.encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        caminho = unquote(self.path)
        inicio, faixa = 0, self.headers.get("Range")
        self.requisicoes.append(("GET", caminho, faixa))
        nome = caminho[len(PREFIXO):]
        if not caminho.startswith(PREFIXO) or nome not in self.arquivos:
            return self.send_error(404)
        dados = self.arquivos[nome]
        if faixa:
            inicio = int(faixa.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {inicio}-{len(dados) - 1}/{len(dados)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(dados) - inicio))
        self.end_headers()
        self.wfile.write(dados[inicio:])


class TestBaixarCnpj(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ThreadingHTTPServer(("127.0.0.1", 0), ServidorFalso)
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.host_original, cls.base_original = app.HOST, app.BASE
        app.HOST = f"http://127.0.0.1:{cls.servidor.server_port}"
        app.BASE = app.HOST + f"/public.php/dav/files/{TOKEN}"

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        app.HOST, app.BASE = cls.host_original, cls.base_original

    def setUp(self):
        ServidorFalso.arquivos = {}
        ServidorFalso.pastas = set()
        ServidorFalso.requisicoes = []
        ServidorFalso.falhas_propfind = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.destino = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def publicar(self, mes, nome, dados):
        ServidorFalso.pastas.add(mes)
        ServidorFalso.arquivos[f"{mes}/{nome}"] = dados

    def rodar(self, funcao, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return funcao(*args, **kwargs)

    # funcionalidade 3: baixar um arquivo isolado
    def test_baixar_arquivo_unico(self):
        self.publicar("2026-10", "Cnaes.zip", b"conteudo-cnaes")
        self.publicar("2026-10", "Socios0.zip", b"conteudo-socios")

        caminho = self.rodar(app.baixar_arquivo, "Cnaes.zip", destino=self.destino)

        self.assertEqual(caminho, self.destino / "2026-10" / "Cnaes.zip")
        self.assertEqual(caminho.read_bytes(), b"conteudo-cnaes")
        self.assertEqual([p.name for p in caminho.parent.iterdir()], ["Cnaes.zip"])

    def test_baixar_arquivo_de_mes_especifico(self):
        self.publicar("2026-09", "Cnaes.zip", b"setembro")
        self.publicar("2026-10", "Cnaes.zip", b"outubro")

        caminho = self.rodar(app.baixar_arquivo, "Cnaes.zip", "2026-09", self.destino)

        self.assertEqual(caminho.read_bytes(), b"setembro")

    def test_arquivo_inexistente(self):
        self.publicar("2026-10", "Cnaes.zip", b"x")
        with self.assertRaises(ValueError):
            self.rodar(app.baixar_arquivo, "NaoExiste.zip", destino=self.destino)

    # funcionalidade 1: baixar os arquivos
    def test_sincronizar_baixa_e_nao_rebaixa(self):
        self.publicar("2026-10", "Cnaes.zip", b"cnaes")
        self.publicar("2026-10", "Empresas0.zip", b"empresas")

        mes, baixados = self.rodar(app.sincronizar, self.destino)

        self.assertEqual(mes, "2026-10")
        self.assertEqual(sorted(baixados), ["Cnaes.zip", "Empresas0.zip"])
        self.assertTrue((self.destino / "2026-10" / "Empresas0.zip").exists())

        ServidorFalso.requisicoes = []
        mes, baixados = self.rodar(app.sincronizar, self.destino)
        self.assertEqual(baixados, [])
        self.assertFalse([r for r in ServidorFalso.requisicoes if r[0] == "GET"])

    # funcionalidade 2: arquivo novo no mesmo mes
    def test_detecta_arquivo_novo_no_mesmo_mes(self):
        self.publicar("2026-10", "Estabelecimentos9.zip", b"nove")
        self.rodar(app.sincronizar, self.destino)

        self.publicar("2026-10", "Estabelecimentos10.zip", b"dez")
        mes, baixados = self.rodar(app.sincronizar, self.destino)

        self.assertEqual(baixados, ["Estabelecimentos10.zip"])
        self.assertEqual((self.destino / mes / "Estabelecimentos10.zip").read_bytes(), b"dez")

    # funcionalidade 2: pasta (mes) nova
    def test_detecta_pasta_nova(self):
        self.publicar("2026-10", "Cnaes.zip", b"outubro")
        self.rodar(app.sincronizar, self.destino)

        self.publicar("2026-11", "Cnaes.zip", b"novembro")
        self.assertEqual(self.rodar(app.ultimo_mes), "2026-11")

        mes, baixados = self.rodar(app.sincronizar, self.destino)

        self.assertEqual((mes, baixados), ("2026-11", ["Cnaes.zip"]))
        self.assertEqual((self.destino / "2026-11" / "Cnaes.zip").read_bytes(), b"novembro")
        self.assertEqual((self.destino / "2026-10" / "Cnaes.zip").read_bytes(), b"outubro")

    # retomada de download interrompido
    def test_retoma_download_pela_metade(self):
        dados = bytes(range(256)) * 40  # 10 KB
        self.publicar("2026-10", "Grande.zip", dados)
        pasta = self.destino / "2026-10"
        pasta.mkdir(parents=True)
        (pasta / "Grande.zip.part").write_bytes(dados[:4096])

        _, baixados = self.rodar(app.sincronizar, self.destino)

        self.assertEqual(baixados, ["Grande.zip"])
        self.assertEqual((pasta / "Grande.zip").read_bytes(), dados)
        self.assertIn("bytes=4096-", [r[2] for r in ServidorFalso.requisicoes if r[0] == "GET"])

    def test_baixar_item_nao_relista_a_pasta(self):
        self.publicar("2026-10", "Cnaes.zip", b"cnaes")
        item = self.rodar(app.listar, "2026-10")[0]
        ServidorFalso.requisicoes = []

        caminho = self.rodar(app.baixar_item, item, "2026-10", self.destino)

        self.assertEqual(caminho.read_bytes(), b"cnaes")
        self.assertEqual([r[0] for r in ServidorFalso.requisicoes], ["GET"])

    def test_listar_retenta_resposta_vazia(self):
        self.publicar("2026-10", "Cnaes.zip", b"cnaes")
        ServidorFalso.falhas_propfind = 1
        with patch.object(app.time, "sleep", lambda _segundos: None):
            itens = self.rodar(app.listar, "2026-10")
        self.assertEqual([item["nome"] for item in itens], ["Cnaes.zip"])
        self.assertEqual(ServidorFalso.falhas_propfind, 0)

    def test_listar_erro_claro_depois_das_tentativas(self):
        self.publicar("2026-10", "Cnaes.zip", b"cnaes")
        ServidorFalso.falhas_propfind = app.TENTATIVAS
        with patch.object(app.time, "sleep", lambda _segundos: None):
            with self.assertRaises(IOError) as contexto:
                self.rodar(app.listar, "2026-10")
        self.assertIn("nao consegui listar", str(contexto.exception))
        self.assertIn("2026-10", str(contexto.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
