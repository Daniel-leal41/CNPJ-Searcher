#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do cnpjs.carregar com zips sinteticos."""

import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from cnpjs import carregar as carga


def linha(cnpj_basico="00000001", ordem="0001", dv="00", fantasia="", situacao="02",
          cnae="4712100", uf="SP", municipio="7107", email="", telefone=""):
    campos = [""] * 30
    campos[0], campos[1], campos[2] = cnpj_basico, ordem, dv
    campos[3] = "1"
    campos[4] = fantasia
    campos[5] = situacao
    campos[6] = "20240101"
    campos[11] = cnae
    campos[19] = uf
    campos[20] = municipio
    campos[26] = telefone
    campos[27] = email
    return ";".join(campos)


def criar_zip(caminho, linhas):
    with zipfile.ZipFile(caminho, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("K3241.K03200Y0.D61010.ESTABELE",
                   ("\r\n".join(linhas) + "\r\n").encode("cp1252"))


def criar_zip_municipios(caminho, itens):
    with zipfile.ZipFile(caminho, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("F.K03200$Z.D61010.MUNICCSV",
                   ("\r\n".join(f"{codigo};{nome}" for codigo, nome in itens)
                    + "\r\n").encode("latin-1"))


class TestCarregarEstabelecimentos(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name) / "2026-10"
        self.pasta.mkdir()
        self.db = Path(self._tmp.name) / "cnpj-2026-10.db"

    def tearDown(self):
        self._tmp.cleanup()

    def carregar(self, **kwargs):
        return carga.carregar(self.pasta, self.db, criar_indices=True, **kwargs)

    def test_junta_shards_em_uma_tabela(self):
        criar_zip(self.pasta / "Estabelecimentos2.zip", [linha("00000002")])
        criar_zip(self.pasta / "Estabelecimentos10.zip", [linha("00000010")])

        self.assertEqual(self.carregar(), 2)

        con = sqlite3.connect(self.db)
        bases = [linha[0] for linha in con.execute(
            "SELECT cnpj_basico FROM estabelecimentos ORDER BY rowid")]
        self.assertEqual(bases, ["00000002", "00000010"])  # 2 antes de 10
        con.close()

    def test_cnpj_completo_e_vazio_vira_null(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [
            linha("00000001", "0001", "09", fantasia="", email=""),
        ])

        self.carregar()

        con = sqlite3.connect(self.db)
        cnpj, basico, fantasia, email = con.execute(
            "SELECT cnpj, cnpj_basico, nome_fantasia, email FROM estabelecimentos").fetchone()
        self.assertEqual(cnpj, "00000001000109")
        self.assertEqual(basico, "00000001")
        self.assertIsNone(fantasia)
        self.assertIsNone(email)
        con.close()

    def test_filtra_ativas_e_por_cnae(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [
            linha("00000001", situacao="02", cnae="4712100"),
            linha("00000002", situacao="08", cnae="4712100"),
            linha("00000003", situacao="02", cnae="5611201"),
        ])

        self.carregar()

        con = sqlite3.connect(self.db)
        ativas = con.execute(
            "SELECT count(*) FROM estabelecimentos WHERE situacao='02'").fetchone()[0]
        cnae = con.execute(
            "SELECT count(*) FROM estabelecimentos WHERE cnae_principal='4712100'").fetchone()[0]
        ambas = con.execute(
            "SELECT count(*) FROM estabelecimentos WHERE situacao='02' "
            "AND cnae_principal='4712100'").fetchone()[0]
        self.assertEqual((ativas, cnae, ambas), (2, 2, 1))
        con.close()

    def test_indices_criados(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [linha()])
        self.carregar()

        con = sqlite3.connect(self.db)
        nomes = {linha[0] for linha in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        con.close()
        self.assertIn("idx_estab_cnae", nomes)
        self.assertIn("idx_estab_situacao", nomes)
        self.assertIn("idx_estab_nome", nomes)
        self.assertIn("idx_estab_email_contato", nomes)
        self.assertIn("idx_estab_telefone", nomes)
        self.assertIn("idx_estab_uf_municipio_situacao", nomes)
        self.assertIn("idx_estab_inicio_situacao", nomes)

    def test_indice_de_nome_atende_like_de_prefixo(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [linha(fantasia="MERCADO")])
        self.carregar()

        con = sqlite3.connect(self.db)
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_estab_nome'").fetchone()[0]
        con.close()
        self.assertIn("COLLATE NOCASE", sql)

    def test_cria_tabela_municipios_com_uf(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [
            linha("00000001", uf="SP", municipio="7107"),
            linha("00000002", uf="MG", municipio="4001"),
        ])
        criar_zip_municipios(self.pasta / "Municipios.zip", [
            ("7107", "SAO PAULO"), ("4001", "ABADIA DOS DOURADOS"),
            ("0001", "GUAJARA-MIRIM"),
        ])

        self.carregar()

        con = sqlite3.connect(self.db)
        linhas = con.execute(
            "SELECT codigo, nome, uf FROM municipios ORDER BY codigo").fetchall()
        con.close()
        self.assertEqual(linhas, [
            ("0001", "GUAJARA-MIRIM", None),
            ("4001", "ABADIA DOS DOURADOS", "MG"),
            ("7107", "SAO PAULO", "SP"),
        ])

    def test_preparar_completa_banco_existente(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [
            linha("00000001", uf="SP", municipio="7107"),
        ])
        carga.carregar(self.pasta, self.db, criar_indices=False)

        criar_zip_municipios(self.pasta / "Municipios.zip", [
            ("7107", "SAO PAULO"),
        ])
        criados = carga.preparar(self.db, self.pasta)
        self.assertIn("municipios", criados)
        self.assertIn("idx_estab_nome", criados)

        con = sqlite3.connect(self.db)
        self.assertEqual(
            con.execute("SELECT nome, uf FROM municipios WHERE codigo='7107'").fetchone(),
            ("SAO PAULO", "SP"))
        nomes = {linha[0] for linha in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        con.close()
        self.assertIn("idx_estab_nome", nomes)
        self.assertEqual(carga.preparar(self.db, self.pasta), [])  # idempotente


    def test_sem_indices(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [linha()])
        carga.carregar(self.pasta, self.db, criar_indices=False)

        con = sqlite3.connect(self.db)
        nomes = {linha[0] for linha in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
        con.close()
        self.assertEqual(nomes, set())

    def test_banco_existente_sem_recriar(self):
        criar_zip(self.pasta / "Estabelecimentos1.zip", [linha()])
        self.carregar()

        with self.assertRaises(FileExistsError):
            self.carregar()
        self.assertEqual(self.carregar(recriar=True), 1)

    def test_linha_com_colunas_erradas(self):
        with zipfile.ZipFile(self.pasta / "Estabelecimentos1.zip", "w") as z:
            z.writestr("x.ESTABELE", "so;tres;colunas\r\n")

        with self.assertRaises(ValueError):
            self.carregar()
        self.assertFalse(self.db.exists())

    def test_pasta_sem_zips(self):
        with self.assertRaises(FileNotFoundError):
            self.carregar()


if __name__ == "__main__":
    unittest.main(verbosity=2)
