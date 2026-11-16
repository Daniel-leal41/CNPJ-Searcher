#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do iniciar.py (sem rede e sem abrir janelas)."""

import queue
import shutil
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import iniciar
from cnpjs import baixar
from tests.test_baixar import ServidorFalso
from tests.test_carregar import criar_zip, criar_zip_municipios, linha


class TestFormatos(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(iniciar.fmt_bytes(500), "500 B")
        self.assertEqual(iniciar.fmt_bytes(2_500_000), "2 MB")
        self.assertEqual(iniciar.fmt_bytes(5_100_000_000), "5,1 GB")

    def test_fmt_tempo(self):
        self.assertEqual(iniciar.fmt_tempo(0.5), "menos de 1 minuto")
        self.assertEqual(iniciar.fmt_tempo(30), "~30 min")
        self.assertEqual(iniciar.fmt_tempo(60), "~1h00")
        self.assertEqual(iniciar.fmt_tempo(90), "~1h30")


class TestEstimativas(unittest.TestCase):
    def test_estimar_download_e_banco(self):
        # 1,4 MB/s por 60 s = 1 minuto de download; 1 GB de zips.
        est = iniciar.estimar(1.4e6 * 60, True, True, 1e9)
        self.assertAlmostEqual(est["min_download"], 1.25)
        self.assertAlmostEqual(est["min_carga"], 3.0)
        self.assertAlmostEqual(est["min_indices"], 3.0)
        self.assertAlmostEqual(est["min_total"], 7.25)

    def test_estimar_sem_trabalho(self):
        est = iniciar.estimar(0, False, False, 5e9)
        self.assertEqual(est["min_total"], 0.0)

    def test_faltantes_consideram_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            (pasta / "Estabelecimentos0.zip").write_bytes(b"0123456789")
            (pasta / "Estabelecimentos1.zip.part").write_bytes(b"0123")
            (pasta / "Estabelecimentos2.zip.part").write_bytes(b"012345678901")
            itens = [
                {"nome": "Estabelecimentos0.zip", "tamanho": 10},
                {"nome": "Estabelecimentos1.zip", "tamanho": 10},
                {"nome": "Estabelecimentos2.zip", "tamanho": 10},
            ]
            faltam, total = iniciar.calcular_faltantes(pasta, itens)
            self.assertEqual([(i["nome"], r) for i, r in faltam],
                             [("Estabelecimentos1.zip", 6),
                              ("Estabelecimentos2.zip", 10)])
            self.assertEqual(total, 16)


class TestArquivos(unittest.TestCase):
    def test_filtrar_necessarios(self):
        itens = [
            {"nome": "Empresas0.zip", "tamanho": 1},
            {"nome": "Socios0.zip", "tamanho": 1},
            {"nome": "Cnaes.zip", "tamanho": 1},
            {"nome": "Municipios.zip", "tamanho": 1},
            {"nome": "Estabelecimentos10.zip", "tamanho": 1},
            {"nome": "Estabelecimentos2.zip", "tamanho": 1},
        ]
        nomes = [i["nome"] for i in iniciar.filtrar_necessarios(itens)]
        self.assertEqual(nomes, [
            "Municipios.zip", "Cnaes.zip",
            "Estabelecimentos2.zip", "Estabelecimentos10.zip"])

    def test_filtrar_necessarios_exige_estabelecimentos(self):
        with self.assertRaises(RuntimeError):
            iniciar.filtrar_necessarios([{"nome": "Municipios.zip", "tamanho": 1}])

    def test_itens_locais_ignora_outros_zips_e_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            (pasta / "Estabelecimentos0.zip").write_bytes(b"x")
            (pasta / "Municipios.zip").write_bytes(b"y")
            (pasta / "Cnaes.zip").write_bytes(b"c")
            (pasta / "Empresas0.zip").write_bytes(b"z")
            (pasta / "Estabelecimentos1.zip.part").write_bytes(b"w")
            itens = iniciar.itens_locais(pasta)
            self.assertEqual([i["nome"] for i in itens],
                             ["Municipios.zip", "Cnaes.zip", "Estabelecimentos0.zip"])
            self.assertEqual(iniciar.arquivos_locais(pasta),
                             {"Municipios.zip": 1, "Cnaes.zip": 1,
                              "Estabelecimentos0.zip": 1})

    def test_extrair_cnae(self):
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            destino = pasta / "CNAE.CNAECSV"
            conteudo = '"5611201";"Restaurantes e similares"\n'.encode("cp1252")
            with zipfile.ZipFile(pasta / "Cnaes.zip", "w") as zip_arquivo:
                zip_arquivo.writestr("F.K03200$Z.D61010.CNAECSV", conteudo)

            self.assertTrue(iniciar.extrair_cnae(pasta, destino))
            self.assertEqual(destino.read_bytes(), conteudo)
            self.assertFalse(iniciar.extrair_cnae(pasta, destino))  # destino mais novo


class TestMeses(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self._tmp.name)
        self._patch = patch.object(iniciar, "DADOS", self.dados)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _criar_mes(self, mes, conteudo=b"x"):
        pasta = self.dados / mes
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / "Estabelecimentos0.zip").write_bytes(conteudo)
        (self.dados / f"cnpj-{mes}.db").write_bytes(conteudo * 2)
        (self.dados / f"cnpj-{mes}.json").write_text("{}")
        return pasta

    def test_meses_antigos_e_tamanho(self):
        self._criar_mes("2026-09", b"a" * 10)
        self._criar_mes("2026-10", b"b" * 5)
        self.assertEqual(iniciar.meses_antigos("2026-10"), ["2026-09"])
        self.assertEqual(iniciar.tamanho_mes("2026-09"), 10 + 20 + 2)
        self.assertEqual(iniciar.tamanho_mes("2026-10"), 5 + 10 + 2)

    def test_apagar_mes_remove_pasta_banco_e_estado(self):
        pasta = self._criar_mes("2026-09", b"a" * 10)
        liberado = iniciar.apagar_mes("2026-09")
        self.assertEqual(liberado, 10 + 20 + 2)
        self.assertFalse(pasta.exists())
        self.assertFalse((self.dados / "cnpj-2026-09.db").exists())
        self.assertFalse((self.dados / "cnpj-2026-09.json").exists())

    def test_apagar_mes_recusa_nome_invalido(self):
        for nome in ("..", "2026-10/../..", "", "2026-9"):
            with self.assertRaises(ValueError):
                iniciar.apagar_mes(nome)

    def test_meses_locais_conta_banco_sem_pasta(self):
        (self.dados / "cnpj-2026-10.db").write_bytes(b"x")
        (self.dados / "cnpj-2026-09.json").write_text("{}")
        self.assertEqual(iniciar.meses_locais(), ["2026-10"])


class TestTexto(unittest.TestCase):
    def test_texto_tem_estimativa_e_cafe(self):
        estado = {
            "mes": "2026-10",
            "faltam_bytes": 2.3e9,
            "faltam_estab": True,
            "falta_municipios": False,
            "mensagens": ["Espaco livre: 300 GB."],
            "estimativa": {
                "min_download": 27.0,
                "min_carga": 15.0,
                "min_indices": 15.0,
                "min_total": 57.0,
            },
        }
        texto = iniciar.montar_texto(estado)
        self.assertIn("Faltam 2,3 GB", texto)
        self.assertIn("Total estimado: ~57 min", texto)
        self.assertIn("shard novo", texto)
        self.assertIn("Pegue um cafe", texto)
        self.assertIn("Espaco livre", texto)

    def test_texto_novo_mes_tem_aviso_e_opcao_de_apagar(self):
        estado = {
            "mes": "2026-11",
            "mes_remoto": "2026-11",
            "mes_local": "2026-10",
            "novo_mes": True,
            "meses_antigos": ["2026-10"],
            "bytes_antigos": 36e9,
            "faltam_bytes": 7.3e9,
            "faltam_estab": True,
            "falta_municipios": True,
            "mensagens": ["Espaco livre: 300 GB."],
            "estimativa": {
                "min_download": 90.0,
                "min_carga": 30.0,
                "min_indices": 30.0,
                "min_total": 150.0,
            },
        }
        texto = iniciar.montar_texto_novo_mes(estado)
        self.assertIn("publicou a nova pasta 2026-11", texto)
        self.assertIn("copia local mais recente e 2026-10", texto)
        self.assertIn("apagar 2026-10", texto)
        self.assertIn("36,0 GB", texto)
        self.assertIn("A exclusao so acontece depois", texto)
        self.assertIn("Pegue um cafe", texto)


class TestOrquestracao(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self._tmp.name) / "dados"
        self.pasta = self.dados / "2026-10"
        self.pasta.mkdir(parents=True)
        self.db = self.dados / "cnpj-2026-10.db"
        self._patch = patch.object(iniciar, "DADOS", self.dados)
        self._patch.start()
        criar_zip(self.pasta / "Estabelecimentos1.zip", [
            linha("00000001", fantasia="PADARIA", uf="SP", municipio="7107"),
        ])
        criar_zip_municipios(self.pasta / "Municipios.zip", [("7107", "SAO PAULO")])

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _estado(self):
        itens = iniciar.itens_locais(self.pasta)
        arquivos = iniciar.arquivos_locais(self.pasta)
        faltam, faltam_bytes = iniciar.calcular_faltantes(self.pasta, itens)
        return {
            "mes": "2026-10",
            "pasta": str(self.pasta),
            "db": str(self.db),
            "itens": itens,
            "faltam": faltam,
            "faltam_bytes": faltam_bytes,
            "faltam_estab": False,
            "falta_municipios": False,
            "precisa_carga": True,
            "precisa_indices": True,
            "pronto": False,
            "info": {},
            "estado": {},
            "arquivos": arquivos,
            "estimativa": {},
            "mensagens": [],
            "remoto": False,
        }

    def _rodar(self):
        fila = queue.Queue()
        iniciar.executar(self._estado(), False, fila)
        mensagens = []
        while not fila.empty():
            mensagens.append(fila.get())
        return mensagens

    def _linhas(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT count(*) FROM estabelecimentos").fetchone()[0]
        finally:
            con.close()

    def _criar_mes_antigo(self):
        antigo = self.dados / "2026-09"
        antigo.mkdir()
        (antigo / "Estabelecimentos0.zip").write_bytes(b"x" * 10)
        (self.dados / "cnpj-2026-09.db").write_bytes(b"y" * 20)
        (self.dados / "cnpj-2026-09.json").write_text("{}")
        return antigo

    def test_carga_retoma_e_recarrega_com_shard_novo(self):
        self._rodar()
        self.assertEqual(self._linhas(), 1)
        estado = iniciar.ler_estado("2026-10")
        self.assertTrue(estado["concluido"])
        self.assertFalse(estado["carregando"])
        self.assertEqual(estado["linhas"], 1)
        self.assertEqual(len(estado["arquivos"]), 2)

        mensagens = self._rodar()  # nada mudou: nao deve recarregar
        self.assertFalse(any(tipo == "log" and "Banco anterior movido" in texto
                             for tipo, texto in mensagens))
        self.assertEqual(self._linhas(), 1)

        # Simula uma carga interrompida: o proximo inicio deve recriar.
        atual = iniciar.ler_estado("2026-10")
        iniciar.salvar_estado("2026-10", atual["arquivos"], None,
                              concluido=False, carregando=True)
        mensagens = self._rodar()
        self.assertTrue(any(tipo == "log" and "Banco anterior movido" in texto
                            for tipo, texto in mensagens))
        self.assertFalse(iniciar.ler_estado("2026-10")["carregando"])
        self.assertEqual(self._linhas(), 1)
        self.assertEqual(list(self.dados.glob("cnpj-2026-10.db.bak-*")), [])

        criar_zip(self.pasta / "Estabelecimentos2.zip", [
            linha("00000002", fantasia="MERCADO", uf="SP", municipio="7107"),
        ])
        mensagens = self._rodar()
        self.assertTrue(any(tipo == "log" and "Banco anterior movido" in texto
                            for tipo, texto in mensagens))
        self.assertEqual(self._linhas(), 2)
        self.assertEqual(iniciar.ler_estado("2026-10")["linhas"], 2)
        self.assertEqual(list(self.dados.glob("cnpj-2026-10.db.bak-*")), [])

        # Remover um zip local nao pode apagar dados de um banco ja completo.
        (self.pasta / "Estabelecimentos2.zip").unlink()
        mensagens = self._rodar()
        self.assertFalse(any(tipo == "log" and "Banco anterior movido" in texto
                             for tipo, texto in mensagens))
        self.assertEqual(self._linhas(), 2)
        self.assertEqual(len(iniciar.ler_estado("2026-10")["arquivos"]), 3)

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute("SELECT nome, uf FROM municipios WHERE codigo='7107'").fetchone(),
                ("SAO PAULO", "SP"))
        finally:
            con.close()

    def test_municipios_entra_sem_recarregar(self):
        (self.pasta / "Municipios.zip").unlink()
        self._rodar()
        self.assertEqual(self._linhas(), 1)
        con = sqlite3.connect(self.db)
        try:
            tabelas = {linha[0] for linha in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertNotIn("municipios", tabelas)

        criar_zip_municipios(self.pasta / "Municipios.zip", [("7107", "SAO PAULO")])
        mensagens = self._rodar()
        self.assertFalse(any(tipo == "log" and "Banco anterior movido" in texto
                             for tipo, texto in mensagens))
        self.assertEqual(self._linhas(), 1)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute("SELECT nome, uf FROM municipios WHERE codigo='7107'").fetchone(),
                ("SAO PAULO", "SP"))
        finally:
            con.close()

    def test_apaga_meses_antigos_so_quando_pedido(self):
        antigo = self._criar_mes_antigo()

        # Sem a caixa marcada, nada e apagado.
        self._rodar()
        self.assertTrue(antigo.exists())
        self.assertTrue((self.dados / "cnpj-2026-09.db").exists())

        estado = self._estado()
        estado["meses_antigos"] = ["2026-09"]
        fila = queue.Queue()
        iniciar.executar(estado, False, fila, apagar_antigos=True)
        self.assertFalse(antigo.exists())
        self.assertFalse((self.dados / "cnpj-2026-09.db").exists())
        self.assertFalse((self.dados / "cnpj-2026-09.json").exists())
        self.assertTrue(self.db.exists())
        self.assertEqual(self._linhas(), 1)

        mensagens = []
        while not fila.empty():
            mensagens.append(fila.get())
        self.assertTrue(any(
            tipo == "log" and texto.startswith("Apagado 2026-09")
            for tipo, texto in mensagens))

    def test_falha_na_carga_preserva_meses_antigos(self):
        antigo = self._criar_mes_antigo()
        estado = self._estado()
        estado["meses_antigos"] = ["2026-09"]
        with patch.object(iniciar.carregar, "carregar",
                          side_effect=RuntimeError("falhou")):
            with self.assertRaises(RuntimeError):
                iniciar.executar(estado, False, queue.Queue(),
                                 apagar_antigos=True)
        self.assertTrue(antigo.exists())
        self.assertTrue((self.dados / "cnpj-2026-09.db").exists())


class TestDownloadOrquestracao(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ThreadingHTTPServer(("127.0.0.1", 0), ServidorFalso)
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.host_original, cls.base_original = baixar.HOST, baixar.BASE
        baixar.HOST = f"http://127.0.0.1:{cls.servidor.server_port}"
        baixar.BASE = baixar.HOST + "/public.php/dav/files/teste"

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        baixar.HOST, baixar.BASE = cls.host_original, cls.base_original

    def setUp(self):
        ServidorFalso.arquivos = {}
        ServidorFalso.pastas = set()
        ServidorFalso.requisicoes = []
        self._tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self._tmp.name) / "dados"
        self.pasta = self.dados / "2026-10"
        self.db = self.dados / "cnpj-2026-10.db"
        self.cnae = Path(self._tmp.name) / "CNAE.CNAECSV"
        self._patch = patch.object(iniciar, "DADOS", self.dados)
        self._patch.start()
        self._patch_cnae = patch.object(iniciar, "CNAE", self.cnae)
        self._patch_cnae.start()

        ServidorFalso.pastas.add("2026-10")
        ServidorFalso.arquivos["2026-10/Estabelecimentos1.zip"] = self._zip(
            [linha("00000001", fantasia="PADARIA", uf="SP", municipio="7107")])
        ServidorFalso.arquivos["2026-10/Municipios.zip"] = self._zip(
            None, municipios=[("7107", "SAO PAULO")])
        ServidorFalso.arquivos["2026-10/Cnaes.zip"] = self._zip_cnae()
        ServidorFalso.arquivos["2026-10/Empresas0.zip"] = b"nao deve ser baixado"

    def tearDown(self):
        self._patch_cnae.stop()
        self._patch.stop()
        self._tmp.cleanup()

    @staticmethod
    def _zip(linhas, municipios=None):
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as arquivo:
            caminho = Path(arquivo.name)
        try:
            if municipios is not None:
                criar_zip_municipios(caminho, municipios)
            else:
                criar_zip(caminho, linhas)
            return caminho.read_bytes()
        finally:
            caminho.unlink(missing_ok=True)

    @staticmethod
    def _zip_cnae():
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as arquivo:
            caminho = Path(arquivo.name)
        try:
            with zipfile.ZipFile(caminho, "w") as zip_arquivo:
                zip_arquivo.writestr(
                    "F.K03200$Z.D61010.CNAECSV",
                    '"5611201";"Restaurantes e similares"\n'.encode("cp1252"))
            return caminho.read_bytes()
        finally:
            caminho.unlink(missing_ok=True)

    def _publicar_mes(self, mes):
        ServidorFalso.pastas.add(mes)
        ServidorFalso.arquivos[f"{mes}/Estabelecimentos1.zip"] = self._zip(
            [linha("00000001", fantasia="PADARIA", uf="SP", municipio="7107")])
        ServidorFalso.arquivos[f"{mes}/Municipios.zip"] = self._zip(
            None, municipios=[("7107", "SAO PAULO")])
        ServidorFalso.arquivos[f"{mes}/Cnaes.zip"] = self._zip_cnae()

    def _baixar_mes_local(self, mes):
        pasta = self.dados / mes
        pasta.mkdir(parents=True, exist_ok=True)
        for nome in ("Estabelecimentos1.zip", "Municipios.zip", "Cnaes.zip"):
            (pasta / nome).write_bytes(ServidorFalso.arquivos[f"{mes}/{nome}"])
        return pasta

    def test_avaliar_avisa_quando_aparece_pasta_nova(self):
        self._baixar_mes_local("2026-10")
        self._publicar_mes("2026-11")

        estado = iniciar.avaliar()

        self.assertTrue(estado["novo_mes"])
        self.assertEqual(estado["mes"], "2026-11")
        self.assertEqual(estado["mes_remoto"], "2026-11")
        self.assertEqual(estado["mes_local"], "2026-10")
        self.assertIn("2026-10", estado["meses_antigos"])
        self.assertGreater(estado["bytes_antigos"], 0)
        self.assertGreater(estado["faltam_bytes"], 0)
        self.assertFalse(estado["pronto"])

    def test_avaliar_com_mes_preferido_continua_no_antigo(self):
        self._baixar_mes_local("2026-10")
        self._publicar_mes("2026-11")

        estado = iniciar.avaliar("2026-10")

        self.assertFalse(estado["novo_mes"])
        self.assertEqual(estado["mes"], "2026-10")
        self.assertEqual(estado["mes_remoto"], "2026-11")
        self.assertEqual(estado["faltam_bytes"], 0)
        self.assertEqual(
            [item["nome"] for item in estado["itens"]],
            ["Municipios.zip", "Cnaes.zip", "Estabelecimentos1.zip"])

    def test_continuar_com_banco_mesmo_sem_os_zips(self):
        pasta = self._baixar_mes_local("2026-10")
        itens = iniciar.itens_locais(pasta)
        estado = {
            "mes": "2026-10",
            "pasta": str(pasta),
            "db": str(self.db),
            "itens": itens,
            "faltam": [],
            "faltam_bytes": 0,
            "faltam_estab": False,
            "falta_municipios": False,
            "precisa_carga": True,
            "precisa_indices": True,
            "pronto": False,
            "carregando": False,
            "info": {},
            "estado": {},
            "arquivos": {},
            "estimativa": {},
            "mensagens": [],
            "remoto": False,
        }
        iniciar.executar(estado, False, queue.Queue())
        self.assertTrue(self.db.exists())

        shutil.rmtree(pasta)
        self._publicar_mes("2026-11")

        estado = iniciar.avaliar("2026-10")
        self.assertTrue(estado["pronto"])
        self.assertFalse(estado["novo_mes"])
        self.assertEqual(estado["mes"], "2026-10")
        self.assertEqual(estado["faltam_bytes"], 0)
        self.assertEqual(estado["meses_antigos"], [])
        self.assertTrue(any("Sem zips de 2026-10" in texto
                            for texto in estado["mensagens"]))

        fila = queue.Queue()
        iniciar.executar(estado, False, fila)
        mensagens = []
        while not fila.empty():
            mensagens.append(fila.get())
        self.assertTrue(any(
            tipo == "log" and "Sem zips locais" in texto
            for tipo, texto in mensagens))
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute("SELECT count(*) FROM estabelecimentos").fetchone()[0], 1)
        finally:
            con.close()

    def test_baixa_so_o_necessario_e_monta_o_banco(self):
        itens = iniciar.filtrar_necessarios(baixar.listar("2026-10"))
        self.assertEqual([item["nome"] for item in itens],
                         ["Municipios.zip", "Cnaes.zip", "Estabelecimentos1.zip"])
        faltam, faltam_bytes = iniciar.calcular_faltantes(self.pasta, itens)
        ServidorFalso.requisicoes = []
        estado = {
            "mes": "2026-10",
            "pasta": str(self.pasta),
            "db": str(self.db),
            "itens": itens,
            "faltam": faltam,
            "faltam_bytes": faltam_bytes,
            "faltam_estab": True,
            "falta_municipios": True,
            "precisa_carga": True,
            "precisa_indices": True,
            "pronto": False,
            "carregando": False,
            "info": {},
            "estado": {},
            "arquivos": {},
            "estimativa": {},
            "mensagens": [],
            "remoto": True,
        }

        fila = queue.Queue()
        iniciar.executar(estado, True, fila)
        mensagens = []
        while not fila.empty():
            mensagens.append(fila.get())

        self.assertTrue((self.pasta / "Estabelecimentos1.zip").exists())
        self.assertTrue((self.pasta / "Municipios.zip").exists())
        self.assertTrue((self.pasta / "Cnaes.zip").exists())
        self.assertFalse((self.pasta / "Empresas0.zip").exists())
        self.assertEqual(len(ServidorFalso.requisicoes), 6)  # 3 listagens + 3 GET
        self.assertTrue(self.cnae.exists())
        self.assertIn("5611201", self.cnae.read_text(encoding="cp1252"))

        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                con.execute("SELECT count(*) FROM estabelecimentos").fetchone()[0], 1)
            self.assertEqual(
                con.execute("SELECT nome, uf FROM municipios WHERE codigo='7107'").fetchone(),
                ("SAO PAULO", "SP"))
        finally:
            con.close()
        self.assertTrue(iniciar.ler_estado("2026-10")["concluido"])
        self.assertTrue(any(tipo == "pronto" for tipo, _dado in mensagens))


if __name__ == "__main__":
    unittest.main(verbosity=2)
