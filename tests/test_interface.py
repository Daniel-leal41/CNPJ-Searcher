#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes da montagem de filtros e auxiliares da interface (sem abrir a janela)."""

import unittest

from cnpjs import interface


class TestFiltros(unittest.TestCase):
    def test_sem_filtros(self):
        self.assertEqual(interface.montar_filtros(), ("", []))

    def test_cnpj_com_pontuacao(self):
        where, params = interface.montar_filtros(cnpj="12.345.678/0001-90")
        self.assertEqual(where, " WHERE e.cnpj LIKE ?")
        self.assertEqual(params, ["12345678000190%"])

    def test_nome_busca_por_prefixo_em_maiusculas(self):
        where, params = interface.montar_filtros(nome="  padaria  ")
        self.assertEqual(where, " WHERE e.nome_fantasia LIKE ?")
        self.assertEqual(params, ["PADARIA%"])

    def test_cnae_multiplo_por_codigo(self):
        where, params = interface.montar_filtros(
            cnaes=["5611-2/01", "4712100", "5611201"])
        self.assertEqual(
            where, " WHERE e.cnae_principal IN ('4712100','5611201')")
        self.assertEqual(params, [])

    def test_situacao_padrao_da_tela(self):
        where, params = interface.montar_filtros(situacao="02")
        self.assertEqual(where, " WHERE e.situacao = ?")
        self.assertEqual(params, ["02"])

    def test_uf_municipio_e_tipo(self):
        where, params = interface.montar_filtros(
            uf="SP", municipio="7107", tipo="1")
        self.assertEqual(
            where,
            " WHERE e.uf = ? AND e.municipio = ? AND e.matriz_filial = ?")
        self.assertEqual(params, ["SP", "7107", "1"])

    def test_intervalo_de_data(self):
        where, params = interface.montar_filtros(
            inicio_de="01/01/2020", inicio_ate="31/12/2020")
        self.assertEqual(
            where,
            " WHERE e.data_inicio_atividade >= ?"
            " AND e.data_inicio_atividade <= ?")
        self.assertEqual(params, ["20200101", "20201231"])

    def test_tem_email_e_telefone(self):
        where, params = interface.montar_filtros(tem_email=True, tem_telefone=True)
        self.assertEqual(
            where,
            " WHERE e.email IS NOT NULL"
            " AND COALESCE(e.telefone1, e.telefone2, e.telefone3) IS NOT NULL")
        self.assertEqual(params, [])

    def test_combinado(self):
        where, params = interface.montar_filtros(
            cnpj="123", nome="mercado", situacao="02", cnaes=["4712100"],
            uf="SP", municipio="7107", tipo="2",
            inicio_de="2020-01-01", inicio_ate="20200131",
            tem_email=True, tem_telefone=True)
        self.assertEqual(
            where,
            " WHERE e.cnpj LIKE ? AND e.nome_fantasia LIKE ? AND e.situacao = ?"
            " AND e.cnae_principal IN ('4712100') AND e.uf = ?"
            " AND e.municipio = ? AND e.matriz_filial = ?"
            " AND e.data_inicio_atividade >= ?"
            " AND e.data_inicio_atividade <= ?"
            " AND e.email IS NOT NULL"
            " AND COALESCE(e.telefone1, e.telefone2, e.telefone3) IS NOT NULL",
        )
        self.assertEqual(
            params,
            ["123%", "MERCADO%", "02", "SP", "7107", "2", "20200101", "20200131"])


class TestAuxiliares(unittest.TestCase):
    def test_normalizar(self):
        self.assertEqual(interface.normalizar("Açaí"), "ACAI")
        self.assertEqual(interface.normalizar(None), "")

    def test_converter_data(self):
        self.assertEqual(interface.converter_data("01/01/2020"), "20200101")
        self.assertEqual(interface.converter_data("31-12-2020"), "20201231")
        self.assertEqual(interface.converter_data("2020-01-31"), "20200131")
        self.assertEqual(interface.converter_data("20200131"), "20200131")
        self.assertEqual(interface.converter_data(""), "")
        with self.assertRaises(ValueError):
            interface.converter_data("31/02/2020")

    def test_formatar_data(self):
        self.assertEqual(interface.formatar_data("20240131"), "31/01/2024")
        self.assertEqual(interface.formatar_data(None), None)

    def test_descricao_situacao(self):
        self.assertEqual(interface.descricao_situacao("02"), "02 - Ativa")
        self.assertEqual(interface.descricao_situacao("99"), "99 - Desconhecida")
        self.assertEqual(interface.descricao_situacao(None), "-")

    def test_filtrar_cnaes_por_descricao(self):
        cnaes = {
            "5611201": "Restaurantes e similares",
            "4712100": "Comercio varejista de mercadorias em geral",
        }
        self.assertEqual(interface.filtrar_cnaes(cnaes, "resta"),
                         [("5611201", "Restaurantes e similares")])
        self.assertEqual(interface.filtrar_cnaes(cnaes, "MERCADORIAS"),
                         [("4712100", "Comercio varejista de mercadorias em geral")])
        self.assertEqual(interface.filtrar_cnaes(cnaes, "inexistente"), [])

    def test_filtrar_cnaes_ignora_acento(self):
        cnaes = {"0111301": "Cultivo de arroz", "0111302": "Cultivo de milho"}
        self.assertEqual(interface.filtrar_cnaes(cnaes, "milho"),
                         [("0111302", "Cultivo de milho")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
