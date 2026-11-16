#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do cnpjs.inspecionar (nao usam a rede)."""

import io
import unittest
import zipfile

from cnpjs import inspecionar as ins


def criar_zip(nome, texto, codificacao="cp1252", metodo=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", metodo) as z:
        z.writestr(nome, texto.encode(codificacao))
    return buffer.getvalue()


class TestLerZipBytes(unittest.TestCase):
    def test_le_latin1_ponto_e_virgula(self):
        texto = ("00000001;EMPRESA AÇÃO LTDA;2062;49;0;05;\r\n"
                 "00000002;OUTRA EMPRESA;2135;22;1000;01;\r\n")
        info = ins.ler_zip_bytes(criar_zip("F.K03200$Z.D61010.EMPRECSV", texto))

        self.assertEqual(info["interno"], "F.K03200$Z.D61010.EMPRECSV")
        self.assertEqual(info["codificacao"], "latin-1")
        self.assertEqual(info["delimitador"], ";")
        self.assertEqual(info["colunas"], [7, 7])
        self.assertEqual(info["linhas"][0][1], "EMPRESA AÇÃO LTDA")
        self.assertEqual(info["tamanho_csv"], len(texto.encode("cp1252")))

    def test_le_ascii(self):
        texto = "00000001;EMPRESA SEM ACENTO;2062;49;0;05;\r\n"
        info = ins.ler_zip_bytes(criar_zip("x.csv", texto))

        self.assertEqual(info["codificacao"], "ascii")
        self.assertEqual(info["linhas"][0][1], "EMPRESA SEM ACENTO")

    def test_le_utf8_com_acento(self):
        texto = "00000001;EMPRESA AÇÃO;2062;49;0;05;\n"
        info = ins.ler_zip_bytes(criar_zip("x.csv", texto, "utf-8"))

        self.assertEqual(info["codificacao"], "utf-8")
        self.assertEqual(info["linhas"][0][1], "EMPRESA AÇÃO")

    def test_le_zip_parcial(self):
        texto = "".join(f"{i:08d};EMPRESA {i};2062;49;0;05;\r\n" for i in range(500))
        dados = criar_zip("grande.csv", texto)

        info = ins.ler_zip_bytes(dados[:400])

        self.assertEqual(info["colunas"], [7, 7, 7])
        self.assertEqual(info["linhas"][0][0], "00000000")

    def test_detecta_virgula(self):
        info = ins.ler_zip_bytes(criar_zip("x.csv", "a,b,c\n1,2,3\n"))
        self.assertEqual(info["delimitador"], ",")
        self.assertEqual(info["colunas"], [3, 3])

    def test_nao_e_zip(self):
        with self.assertRaises(ValueError):
            ins.ler_zip_bytes(b"isso nao e um zip")

    def test_zip_vazio(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("vazio.csv", b"")
        with self.assertRaises(ValueError):
            ins.ler_zip_bytes(buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
