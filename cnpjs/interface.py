#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interface Tkinter para consultar o banco de Estabelecimentos em SQLite.

Uso:
    python -m cnpjs.interface
    python -m cnpjs.interface --db dados/teste-estabelecimentos0.db

Os filtros viram um WHERE no SQLite; os resultados sao paginados de 100 em 100.
O filtro de nome usa LIKE 'texto%' com o indice COLLATE NOCASE (sem full scan).
"""

import argparse
import csv
import datetime
import re
import sqlite3
import tkinter as tk
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

LIMITE = 100
TETO_EXPORTACAO = 200_000

SITUACOES = {"01": "Nula", "02": "Ativa", "03": "Suspensa", "04": "Inapta", "08": "Baixada"}
TIPOS = {"1": "Matriz", "2": "Filial"}
UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
       "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP",
       "TO", "EX"]


def descricao_situacao(codigo):
    if not codigo:
        return "-"
    return f"{codigo} - {SITUACOES.get(codigo, 'Desconhecida')}"


def normalizar(texto):
    """Remove acentos e coloca em maiusculas, para busca por descricao."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c)).upper()


def converter_data(texto):
    """Aceita DD/MM/AAAA, DD-MM-AAAA, AAAA-MM-DD ou AAAAMMDD -> AAAAMMDD."""
    texto = (texto or "").strip()
    if not texto:
        return ""
    for formato in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(texto, formato).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"data invalida: {texto!r} (use DD/MM/AAAA)")


def formatar_data(valor):
    if valor and len(valor) == 8 and valor.isdigit():
        return f"{valor[6:8]}/{valor[4:6]}/{valor[0:4]}"
    return valor


def _telefone_sql(numero):
    return (f"NULLIF(TRIM(COALESCE(e.ddd{numero}, '') || ' ' || "
            f"COALESCE(e.telefone{numero}, '')), '')")


@dataclass(frozen=True)
class Coluna:
    chave: str
    titulo: str
    sql: str
    largura: int


COLUNAS = [
    Coluna("cnpj", "CNPJ", "e.cnpj", 145),
    Coluna("nome", "Nome fantasia", "e.nome_fantasia", 300),
    Coluna("situacao", "Situacao", "e.situacao", 115),
    Coluna("tipo", "Tipo", "e.matriz_filial", 75),
    Coluna("cnae", "CNAE", "e.cnae_principal", 80),
    Coluna("cnae_desc", "CNAE (descricao)", "e.cnae_principal", 250),
    Coluna("uf", "UF", "e.uf", 40),
    Coluna("municipio", "Municipio", "COALESCE(m.nome, e.municipio)", 160),
    Coluna("bairro", "Bairro", "e.bairro", 140),
    Coluna("logradouro", "Logradouro", "e.logradouro", 180),
    Coluna("numero", "Numero", "e.numero", 75),
    Coluna("complemento", "Complemento", "e.complemento", 120),
    Coluna("cep", "CEP", "e.cep", 85),
    Coluna("telefone1", "Telefone 1", _telefone_sql(1), 125),
    Coluna("telefone2", "Telefone 2", _telefone_sql(2), 125),
    Coluna("telefone3", "Telefone 3", _telefone_sql(3), 125),
    Coluna("email", "E-mail", "e.email", 190),
    Coluna("inicio", "Inicio atividade", "e.data_inicio_atividade", 110),
    Coluna("situacao_especial", "Situacao especial", "e.situacao_especial", 120),
    Coluna("data_situacao_especial", "Data situacao especial",
           "e.data_situacao_especial", 140),
]
MAPA_COLUNAS = {coluna.chave: coluna for coluna in COLUNAS}
COLUNAS_PADRAO = ["cnpj", "nome", "situacao", "cnae", "uf", "municipio", "inicio"]


def montar_filtros(cnpj="", nome="", situacao="", cnaes=(), uf="", municipio="",
                   tipo="", inicio_de="", inicio_ate="", tem_email=False,
                   tem_telefone=False):
    """Monta o WHERE e os parametros a partir dos campos da tela."""
    condicoes, parametros = [], []

    numero = re.sub(r"\D", "", cnpj)
    if numero:
        condicoes.append("e.cnpj LIKE ?")
        parametros.append(numero + "%")

    nome = nome.strip()
    if nome:
        # LIKE 'texto%' (prefixo) usa idx_estab_nome; %texto% faria full scan.
        condicoes.append("e.nome_fantasia LIKE ?")
        parametros.append(nome.upper() + "%")

    if situacao:
        condicoes.append("e.situacao = ?")
        parametros.append(situacao)

    codigos = sorted({re.sub(r"\D", "", codigo) for codigo in cnaes if re.sub(r"\D", "", codigo)})
    if codigos:
        # Codigos sao validados como digitos; literais evitam o limite de ? do SQLite.
        condicoes.append("e.cnae_principal IN (" + ",".join(f"'{c}'" for c in codigos) + ")")

    if uf:
        condicoes.append("e.uf = ?")
        parametros.append(uf)

    if municipio:
        condicoes.append("e.municipio = ?")
        parametros.append(municipio)

    if tipo:
        condicoes.append("e.matriz_filial = ?")
        parametros.append(tipo)

    inicio_de = converter_data(inicio_de)
    if inicio_de:
        condicoes.append("e.data_inicio_atividade >= ?")
        parametros.append(inicio_de)

    inicio_ate = converter_data(inicio_ate)
    if inicio_ate:
        condicoes.append("e.data_inicio_atividade <= ?")
        parametros.append(inicio_ate)

    if tem_email:
        condicoes.append("e.email IS NOT NULL")

    if tem_telefone:
        condicoes.append(
            "COALESCE(e.telefone1, e.telefone2, e.telefone3) IS NOT NULL")

    where = (" WHERE " + " AND ".join(condicoes)) if condicoes else ""
    return where, parametros


def filtrar_cnaes(cnaes, termo=""):
    """Lista (codigo, descricao) cuja descricao contem o termo (sem acento)."""
    alvo = normalizar(termo)
    itens = sorted(cnaes.items())
    if not alvo:
        return itens
    return [(codigo, descricao) for codigo, descricao in itens
            if alvo in normalizar(descricao) or alvo in codigo]


def carregar_cnaes():
    """Codigo -> descricao, a partir do CNAE.CNAECSV na raiz do projeto."""
    caminho = Path(__file__).resolve().parent.parent / "CNAE.CNAECSV"
    mapa = {}
    if caminho.exists():
        with open(caminho, encoding="cp1252", newline="") as arquivo:
            for linha in csv.reader(arquivo, delimiter=";", quotechar='"'):
                if len(linha) >= 2:
                    mapa[linha[0].strip()] = linha[1].strip()
    return mapa


def carregar_municipios(con):
    """UF -> [(codigo, nome)], a partir da tabela municipios."""
    try:
        linhas = con.execute(
            "SELECT uf, codigo, nome FROM municipios "
            "WHERE uf IS NOT NULL ORDER BY nome").fetchall()
    except sqlite3.Error:
        return {}
    por_uf = {}
    for uf, codigo, nome in linhas:
        por_uf.setdefault(uf, []).append((codigo, nome))
    return por_uf


def banco_padrao():
    candidatos = sorted(Path("dados").glob("*.db"), key=lambda p: p.stat().st_mtime)
    return candidatos[-1] if candidatos else Path("dados/teste-estabelecimentos0.db")


class DialogoCnaes(tk.Toplevel):
    """Selecao multipla de CNAEs, com busca por descricao."""

    def __init__(self, pai, cnaes, selecionados):
        super().__init__(pai)
        self.title("Selecionar CNAEs")
        self.geometry("700x560")
        self.transient(pai)
        self.grab_set()
        self.resultado = None
        self.cnaes = cnaes
        self.selecionados = set(selecionados)
        self._visiveis = []
        self._atualizando = False
        self._montar()
        self._preencher("")
        self.protocol("WM_DELETE_WINDOW", self._cancelar)
        self.bind("<Escape>", lambda _evento: self._cancelar())

    def _montar(self):
        quadro = ttk.Frame(self, padding=10)
        quadro.pack(fill="both", expand=True)

        ttk.Label(quadro, text="Buscar por descricao:").pack(anchor="w")
        self.e_busca = ttk.Entry(quadro)
        self.e_busca.pack(fill="x", pady=(2, 6))
        self.e_busca.bind("<KeyRelease>", lambda _evento: self._preencher(self.e_busca.get()))
        ttk.Label(quadro, text="Ctrl/Shift para selecionar varios; a busca filtra por descricao.",
                  foreground="#666").pack(anchor="w", pady=(0, 4))

        lista = ttk.Frame(quadro)
        lista.pack(fill="both", expand=True)
        self.lb = tk.Listbox(lista, selectmode="extended", activestyle="none")
        barra = ttk.Scrollbar(lista, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=barra.set)
        self.lb.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.lb.bind("<<ListboxSelect>>", lambda _evento: self._sincronizar())

        self.lbl_total = ttk.Label(quadro, text="", foreground="#555")
        self.lbl_total.pack(anchor="w", pady=(6, 0))

        botoes = ttk.Frame(quadro)
        botoes.pack(fill="x", pady=(8, 0))
        ttk.Button(botoes, text="Selecionar visiveis",
                   command=self._selecionar_visiveis).pack(side="left")
        ttk.Button(botoes, text="Limpar", command=self._limpar).pack(side="left", padx=4)
        ttk.Button(botoes, text="Cancelar", command=self._cancelar).pack(side="right")
        ttk.Button(botoes, text="Aplicar", command=self._aplicar).pack(side="right", padx=4)

    def _sincronizar(self):
        if self._atualizando:
            return
        for indice, (codigo, _descricao) in enumerate(self._visiveis):
            if self.lb.selection_includes(indice):
                self.selecionados.add(codigo)
            else:
                self.selecionados.discard(codigo)

    def _preencher(self, termo):
        self._sincronizar()
        self._visiveis = filtrar_cnaes(self.cnaes, termo)
        self._atualizando = True
        try:
            self.lb.delete(0, "end")
            for codigo, descricao in self._visiveis:
                self.lb.insert("end", f"{codigo} - {descricao}")
                if codigo in self.selecionados:
                    self.lb.selection_set("end")
        finally:
            self._atualizando = False
        self.lbl_total.config(
            text=f"{len(self._visiveis)} CNAE(s) | {len(self.selecionados)} selecionado(s)")

    def _selecionar_visiveis(self):
        self._atualizando = True
        try:
            self.lb.selection_set(0, "end")
            for codigo, _descricao in self._visiveis:
                self.selecionados.add(codigo)
        finally:
            self._atualizando = False
        self.lbl_total.config(
            text=f"{len(self._visiveis)} CNAE(s) | {len(self.selecionados)} selecionado(s)")

    def _limpar(self):
        self._atualizando = True
        try:
            self.selecionados.clear()
            self.lb.selection_clear(0, "end")
        finally:
            self._atualizando = False
        self.lbl_total.config(
            text=f"{len(self._visiveis)} CNAE(s) | 0 selecionado(s)")

    def _aplicar(self):
        self._sincronizar()
        self.resultado = set(self.selecionados)
        self.destroy()

    def _cancelar(self):
        self.resultado = None
        self.destroy()


class DialogoColunas(tk.Toplevel):
    """Escolha das colunas visiveis (e exportadas)."""

    def __init__(self, pai, visiveis):
        super().__init__(pai)
        self.title("Colunas")
        self.transient(pai)
        self.grab_set()
        self.resultado = None
        self.vars = {}
        self._montar(visiveis)
        self.protocol("WM_DELETE_WINDOW", self._cancelar)
        self.bind("<Escape>", lambda _evento: self._cancelar())

    def _montar(self, visiveis):
        quadro = ttk.Frame(self, padding=12)
        quadro.pack(fill="both", expand=True)
        ttk.Label(quadro, text="Colunas exibidas e exportadas:").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for indice, coluna in enumerate(COLUNAS):
            var = tk.BooleanVar(value=coluna.chave in visiveis)
            self.vars[coluna.chave] = var
            ttk.Checkbutton(quadro, text=coluna.titulo, variable=var).grid(
                row=1 + indice // 2, column=indice % 2, sticky="w", padx=(0, 18), pady=2)
        botoes = ttk.Frame(quadro)
        botoes.grid(row=1 + (len(COLUNAS) + 1) // 2, column=0, columnspan=2,
                    sticky="e", pady=(12, 0))
        ttk.Button(botoes, text="Cancelar", command=self._cancelar).pack(side="right")
        ttk.Button(botoes, text="Aplicar", command=self._aplicar).pack(side="right", padx=4)

    def _aplicar(self):
        escolhidas = [coluna.chave for coluna in COLUNAS if self.vars[coluna.chave].get()]
        if not escolhidas:
            messagebox.showwarning("Colunas", "Selecione pelo menos uma coluna.", parent=self)
            return
        self.resultado = escolhidas
        self.destroy()

    def _cancelar(self):
        self.resultado = None
        self.destroy()


class Interface:
    def __init__(self, raiz, db):
        self.raiz = raiz
        self.con = None
        self.total = 0
        self.pagina = 0
        self.cnaes = carregar_cnaes()
        self.cnaes_selecionados = set()
        self.municipios = {}
        self.tem_municipios = False
        self._municipios_atuais = []
        self._limite_municipios = 500
        self.colunas_visiveis = list(COLUNAS_PADRAO)

        raiz.title("Consulta de Estabelecimentos - CNPJ")
        raiz.geometry("1280x760")
        self._montar()
        self.abrir_banco(db)

    # ------------------------------------------------------------- montagem
    def _montar(self):
        topo = ttk.Frame(self.raiz, padding=8)
        topo.pack(fill="x")
        ttk.Label(topo, text="Banco:").pack(side="left")
        self.lbl_banco = ttk.Label(topo, text="(nenhum)", foreground="#444")
        self.lbl_banco.pack(side="left", padx=6)
        ttk.Button(topo, text="Abrir banco...", command=self.escolher_banco).pack(side="right")

        filtros = ttk.LabelFrame(self.raiz, text="Filtros", padding=8)
        filtros.pack(fill="x", padx=8, pady=(0, 4))
        filtros.columnconfigure(1, weight=1)
        filtros.columnconfigure(3, weight=1)
        filtros.columnconfigure(5, weight=1)

        # 1) UF e municipio.
        ttk.Label(filtros, text="UF:").grid(row=0, column=0, sticky="e")
        self.cb_uf = ttk.Combobox(filtros, state="readonly", width=7,
                                  values=["Todas"] + UFS)
        self.cb_uf.set("Todas")
        self.cb_uf.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        self.cb_uf.bind("<<ComboboxSelected>>", lambda _evento: self._uf_mudou())

        ttk.Label(filtros, text="Municipio:").grid(row=0, column=2, sticky="e")
        self.cb_municipio = ttk.Combobox(filtros, width=36)
        self.cb_municipio.grid(row=0, column=3, columnspan=2, sticky="we", padx=4, pady=2)
        self.cb_municipio.bind("<KeyRelease>", self._filtrar_municipios)
        self.cb_municipio.bind("<Return>", lambda _evento: self.pesquisar())
        ttk.Label(filtros, text="digite para filtrar", foreground="#888").grid(
            row=0, column=5, sticky="w", padx=(0, 4))

        # 2) CNAE principal (multiselect por descricao).
        ttk.Label(filtros, text="CNAE principal:").grid(row=1, column=0, sticky="e")
        self.btn_cnae = ttk.Button(filtros, text="Selecionar CNAEs...",
                                   command=self.escolher_cnaes)
        self.btn_cnae.grid(row=1, column=1, sticky="w", padx=4, pady=2)
        self.lbl_cnae = ttk.Label(filtros, text="Todos os CNAEs", foreground="#555",
                                  wraplength=900)
        self.lbl_cnae.grid(row=1, column=2, columnspan=4, sticky="w", padx=4)

        # 3) Situacao (padrao ativa) e 4) nome.
        ttk.Label(filtros, text="Situacao:").grid(row=2, column=0, sticky="e")
        self.cb_situacao = ttk.Combobox(
            filtros, state="readonly", width=18,
            values=["Todas"] + [f"{k} - {v}" for k, v in SITUACOES.items()])
        self.cb_situacao.set("02 - Ativa")
        self.cb_situacao.grid(row=2, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(filtros, text="Nome (fantasia):").grid(row=2, column=2, sticky="e")
        self.e_nome = ttk.Entry(filtros)
        self.e_nome.grid(row=2, column=3, columnspan=3, sticky="we", padx=4, pady=2)
        self.e_nome.bind("<Return>", lambda _evento: self.pesquisar())

        # 5) Tipo, inicio e 6) e-mail/telefone.
        ttk.Label(filtros, text="Tipo:").grid(row=3, column=0, sticky="e")
        self.cb_tipo = ttk.Combobox(filtros, state="readonly", width=10,
                                    values=["Todas", "Matriz", "Filial"])
        self.cb_tipo.set("Todas")
        self.cb_tipo.grid(row=3, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(filtros, text="Inicio de:").grid(row=3, column=2, sticky="e")
        self.e_inicio_de = ttk.Entry(filtros, width=12)
        self.e_inicio_de.grid(row=3, column=3, sticky="w", padx=4, pady=2)
        self.e_inicio_de.bind("<Return>", lambda _evento: self.pesquisar())
        ttk.Label(filtros, text="ate:").grid(row=3, column=4, sticky="e")
        self.e_inicio_ate = ttk.Entry(filtros, width=12)
        self.e_inicio_ate.grid(row=3, column=5, sticky="w", padx=4, pady=2)
        self.e_inicio_ate.bind("<Return>", lambda _evento: self.pesquisar())

        self.var_email = tk.BooleanVar(value=False)
        self.var_telefone = tk.BooleanVar(value=False)
        ttk.Checkbutton(filtros, text="Com e-mail", variable=self.var_email).grid(
            row=3, column=6, sticky="w", padx=(12, 4))
        ttk.Checkbutton(filtros, text="Com telefone", variable=self.var_telefone).grid(
            row=3, column=7, sticky="w", padx=4)
        ttk.Label(filtros, text="(DD/MM/AAAA)", foreground="#888").grid(
            row=3, column=8, sticky="w", padx=(4, 0))

        # CNPJ + acoes.
        ttk.Label(filtros, text="CNPJ:").grid(row=4, column=0, sticky="e")
        self.e_cnpj = ttk.Entry(filtros, width=22)
        self.e_cnpj.grid(row=4, column=1, sticky="w", padx=4, pady=2)
        self.e_cnpj.bind("<Return>", lambda _evento: self.pesquisar())

        botoes = ttk.Frame(filtros)
        botoes.grid(row=4, column=2, columnspan=7, sticky="e", pady=(2, 0))
        ttk.Button(botoes, text="Pesquisar", command=self.pesquisar).pack(side="left")
        ttk.Button(botoes, text="Limpar", command=self.limpar).pack(side="left", padx=4)
        ttk.Button(botoes, text="Exportar CSV...", command=self.exportar).pack(
            side="left", padx=4)
        ttk.Button(botoes, text="Colunas...", command=self.escolher_colunas).pack(
            side="left", padx=4)

        quadro = ttk.Frame(self.raiz, padding=(8, 4))
        quadro.pack(fill="both", expand=True)
        quadro.rowconfigure(0, weight=1)
        quadro.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(quadro, columns=(), show="headings")
        barra_v = ttk.Scrollbar(quadro, orient="vertical", command=self.tree.yview)
        barra_h = ttk.Scrollbar(quadro, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=barra_v.set, xscrollcommand=barra_h.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        barra_v.grid(row=0, column=1, sticky="ns")
        barra_h.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("par", background="#f4f4f4")
        self._montar_tabela()

        rodape = ttk.Frame(self.raiz, padding=8)
        rodape.pack(fill="x")
        self.lbl_status = ttk.Label(rodape, text="Selecione um banco para comecar.")
        self.lbl_status.pack(side="left")
        self.btn_proxima = ttk.Button(rodape, text="Proxima >", command=self.proxima)
        self.btn_proxima.pack(side="right")
        self.btn_anterior = ttk.Button(rodape, text="< Anterior", command=self.anterior)
        self.btn_anterior.pack(side="right", padx=4)

    def _montar_tabela(self):
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = tuple(self.colunas_visiveis)
        for chave in self.colunas_visiveis:
            coluna = MAPA_COLUNAS[chave]
            self.tree.heading(chave, text=coluna.titulo)
            self.tree.column(chave, width=coluna.largura, anchor="w",
                             stretch=chave in ("nome", "cnae_desc", "email"))

    # ---------------------------------------------------------------- banco
    def abrir_banco(self, caminho):
        caminho = Path(caminho)
        if not caminho.exists():
            self.lbl_status.config(
                text=f"Banco nao encontrado: {caminho}. Gere com python -m cnpjs.carregar."
            )
            return
        try:
            con = sqlite3.connect(f"{caminho.resolve().as_uri()}?mode=ro", uri=True)
            con.execute("SELECT count(*) FROM estabelecimentos").fetchone()
        except sqlite3.Error as erro:
            messagebox.showerror("Erro ao abrir", f"{caminho}\n\n{erro}")
            return
        if self.con is not None:
            self.con.close()
        self.con = con
        self.tem_municipios = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='municipios'").fetchone())
        self.municipios = carregar_municipios(con) if self.tem_municipios else {}
        if self.tem_municipios:
            self.cb_municipio.config(state="normal")
        else:
            self.cb_municipio.set("")
            self.cb_municipio["values"] = []
            self.cb_municipio.config(state="disabled")
        self._uf_mudou()
        texto = str(caminho) if self.tem_municipios else f"{caminho} (sem municipios)"
        self.lbl_banco.config(text=texto)
        self.pesquisar()

    def escolher_banco(self):
        caminho = filedialog.askopenfilename(
            title="Escolha o banco SQLite",
            filetypes=[("SQLite", "*.db"), ("Todos", "*")],
        )
        if caminho:
            self.abrir_banco(caminho)

    # ------------------------------------------------------------ municipios
    def _filtro_uf(self):
        return "" if self.cb_uf.get() == "Todas" else self.cb_uf.get()

    def _uf_mudou(self):
        uf = self._filtro_uf()
        if uf:
            self._municipios_atuais = list(self.municipios.get(uf, []))
        else:
            self._municipios_atuais = sorted(
                (codigo, nome) for lista in self.municipios.values()
                for codigo, nome in lista)
        self.cb_municipio.set("")
        self._limite_municipios = 1000 if uf else 500
        self.cb_municipio["values"] = [
            nome for _codigo, nome in self._municipios_atuais][:self._limite_municipios]

    def _filtrar_municipios(self, _evento=None):
        termo = normalizar(self.cb_municipio.get())
        if not termo:
            nomes = self._municipios_atuais[:self._limite_municipios]
        else:
            nomes = [(codigo, nome) for codigo, nome in self._municipios_atuais
                     if termo in normalizar(nome)][:300]
        self.cb_municipio["values"] = [nome for _codigo, nome in nomes]

    def _codigo_municipio(self):
        nome = self.cb_municipio.get().strip()
        if not nome or not self.tem_municipios:
            return ""
        alvo = normalizar(nome)
        exatos = [codigo for codigo, candidato in self._municipios_atuais
                  if normalizar(candidato) == alvo]
        if len(exatos) == 1:
            return exatos[0]
        parciais = [codigo for codigo, candidato in self._municipios_atuais
                    if alvo in normalizar(candidato)]
        if len(parciais) == 1:
            return parciais[0]
        if len(parciais) > 1:
            raise ValueError(f"municipio ambiguo: {nome!r}; escolha na lista")
        raise ValueError(f"municipio nao encontrado: {nome!r}")

    # -------------------------------------------------------------- consulta
    def _filtro_situacao(self):
        valor = self.cb_situacao.get()
        return "" if valor == "Todas" else valor.split(" ")[0]

    def _filtro_tipo(self):
        return {"Matriz": "1", "Filial": "2"}.get(self.cb_tipo.get(), "")

    def _filtros(self):
        return montar_filtros(
            cnpj=self.e_cnpj.get(), nome=self.e_nome.get(),
            situacao=self._filtro_situacao(), cnaes=self.cnaes_selecionados,
            uf=self._filtro_uf(), municipio=self._codigo_municipio(),
            tipo=self._filtro_tipo(), inicio_de=self.e_inicio_de.get(),
            inicio_ate=self.e_inicio_ate.get(), tem_email=self.var_email.get(),
            tem_telefone=self.var_telefone.get())

    def _expressao(self, coluna):
        if not self.tem_municipios and coluna.chave == "municipio":
            return "e.municipio"
        return coluna.sql

    def _select(self, chaves):
        expressoes = [f"{self._expressao(MAPA_COLUNAS[chave])} AS c{indice}"
                      for indice, chave in enumerate(chaves)]
        origem = "estabelecimentos e"
        if self.tem_municipios:
            origem += " LEFT JOIN municipios m ON m.codigo = e.municipio"
        return f"SELECT {', '.join(expressoes)} FROM {origem}"

    def _valor(self, chave, valor, exportar=False):
        if valor is None or valor == "":
            return "" if exportar else "-"
        if chave == "situacao":
            return descricao_situacao(valor)
        if chave == "tipo":
            return TIPOS.get(valor, valor)
        if chave == "cnae_desc":
            return self.cnaes.get(valor, "")
        if chave in ("inicio", "data_situacao_especial"):
            return formatar_data(valor)
        return str(valor)

    def pesquisar(self, nova=True):
        if self.con is None:
            return
        try:
            where, parametros = self._filtros()
        except ValueError as erro:
            messagebox.showerror("Filtro invalido", str(erro))
            return
        if nova:
            self.pagina = 0
        self.status("Pesquisando...")
        self.raiz.update_idletasks()
        try:
            if nova:
                self.total = self.con.execute(
                    "SELECT count(*) FROM estabelecimentos e" + where, parametros
                ).fetchone()[0]
            sql = self._select(self.colunas_visiveis) + where + \
                " LIMIT ? OFFSET ?"
            linhas = self.con.execute(
                sql, [*parametros, LIMITE, self.pagina * LIMITE]).fetchall()
        except sqlite3.Error as erro:
            messagebox.showerror("Erro na consulta", str(erro))
            return

        self.tree.delete(*self.tree.get_children())
        for indice, linha in enumerate(linhas):
            valores = [self._valor(chave, linha[posicao])
                       for posicao, chave in enumerate(self.colunas_visiveis)]
            self.tree.insert("", "end", tags=("par",) if indice % 2 else (),
                             values=valores)
        self._atualizar_status(linhas)

    def _atualizar_status(self, linhas):
        if not self.total:
            self.status("Nenhum resultado.")
        else:
            inicio = self.pagina * LIMITE + 1
            fim = inicio + len(linhas) - 1
            self.status(f"Mostrando {inicio} a {fim} de {self.total:,} resultado(s)."
                        .replace(",", "."))
        self.btn_anterior.config(state="normal" if self.pagina > 0 else "disabled")
        self.btn_proxima.config(
            state="normal" if (self.pagina + 1) * LIMITE < self.total else "disabled")

    def status(self, texto):
        self.lbl_status.config(text=texto)

    def limpar(self):
        self.e_cnpj.delete(0, "end")
        self.e_nome.delete(0, "end")
        self.e_inicio_de.delete(0, "end")
        self.e_inicio_ate.delete(0, "end")
        self.cb_situacao.set("02 - Ativa")
        self.cb_uf.set("Todas")
        self.cb_tipo.set("Todas")
        self.cnaes_selecionados.clear()
        self.var_email.set(False)
        self.var_telefone.set(False)
        self._uf_mudou()
        self._atualizar_rotulo_cnae()
        self.pesquisar()

    def anterior(self):
        if self.pagina > 0:
            self.pagina -= 1
            self.pesquisar(nova=False)

    def proxima(self):
        if (self.pagina + 1) * LIMITE < self.total:
            self.pagina += 1
            self.pesquisar(nova=False)

    # ----------------------------------------------------------------- CNAE
    def escolher_cnaes(self):
        if not self.cnaes:
            messagebox.showwarning(
                "CNAE", "CNAE.CNAECSV nao encontrado na raiz do projeto.")
            return
        dialogo = DialogoCnaes(self.raiz, self.cnaes, self.cnaes_selecionados)
        self.raiz.wait_window(dialogo)
        if dialogo.resultado is not None:
            self.cnaes_selecionados = dialogo.resultado
            self._atualizar_rotulo_cnae()

    def _atualizar_rotulo_cnae(self):
        if not self.cnaes_selecionados:
            self.lbl_cnae.config(text="Todos os CNAEs")
            return
        if len(self.cnaes_selecionados) <= 2:
            partes = [f"{codigo} - {self.cnaes.get(codigo, '?')}"
                      for codigo in sorted(self.cnaes_selecionados)]
            self.lbl_cnae.config(text="; ".join(partes))
        else:
            self.lbl_cnae.config(
                text=f"{len(self.cnaes_selecionados)} CNAEs selecionados")

    # -------------------------------------------------------------- colunas
    def escolher_colunas(self):
        dialogo = DialogoColunas(self.raiz, self.colunas_visiveis)
        self.raiz.wait_window(dialogo)
        if dialogo.resultado:
            self.colunas_visiveis = dialogo.resultado
            self._montar_tabela()
            self.pesquisar(nova=False)

    # ------------------------------------------------------------- exportar
    def exportar(self):
        if self.con is None:
            return
        try:
            where, parametros = self._filtros()
        except ValueError as erro:
            messagebox.showerror("Filtro invalido", str(erro))
            return
        try:
            total = self.con.execute(
                "SELECT count(*) FROM estabelecimentos e" + where, parametros
            ).fetchone()[0]
        except sqlite3.Error as erro:
            messagebox.showerror("Erro na consulta", str(erro))
            return
        if not total:
            messagebox.showinfo("Exportar CSV", "Nenhum resultado para exportar.")
            return

        limite = min(total, TETO_EXPORTACAO)
        mensagem = f"O filtro atual tem {total:,} resultado(s).".replace(",", ".")
        if total > TETO_EXPORTACAO:
            mensagem += (f"\n\nO teto de exportacao e {TETO_EXPORTACAO:,} linhas; "
                         f"serao exportadas as primeiras {TETO_EXPORTACAO:,}.")
        mensagem += "\n\nContinuar?"
        if not messagebox.askyesno("Exportar CSV", mensagem):
            return
        caminho = filedialog.asksaveasfilename(
            title="Salvar CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Todos", "*")],
            initialfile="estabelecimentos.csv",
        )
        if not caminho:
            return
        self._exportar(caminho, where, parametros, limite)

    def _exportar(self, caminho, where, parametros, limite):
        sql = self._select(self.colunas_visiveis) + where + " LIMIT ?"
        progresso = tk.Toplevel(self.raiz)
        progresso.title("Exportando")
        progresso.geometry("380x110")
        progresso.transient(self.raiz)
        progresso.grab_set()
        progresso.resizable(False, False)
        progresso.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(progresso, text=f"Exportando ate {limite:,} linhas...".replace(",", ".")
                  ).pack(pady=(14, 8))
        barra = ttk.Progressbar(progresso, maximum=limite, length=340)
        barra.pack(padx=18)
        escritos = 0
        try:
            cursor = self.con.execute(sql, [*parametros, limite])
            with open(caminho, "w", encoding="utf-8-sig", newline="") as arquivo:
                escritor = csv.writer(arquivo, delimiter=";")
                escritor.writerow([MAPA_COLUNAS[chave].titulo
                                   for chave in self.colunas_visiveis])
                while True:
                    lote = cursor.fetchmany(2000)
                    if not lote:
                        break
                    for linha in lote:
                        escritor.writerow(
                            [self._valor(chave, linha[posicao], exportar=True)
                             for posicao, chave in enumerate(self.colunas_visiveis)])
                    escritos += len(lote)
                    barra["value"] = escritos
                    progresso.update()
        except (OSError, sqlite3.Error, tk.TclError) as erro:
            progresso.destroy()
            Path(caminho).unlink(missing_ok=True)
            messagebox.showerror("Erro ao exportar", str(erro))
            return
        progresso.destroy()
        self.status(f"{escritos:,} resultado(s) exportado(s) para {caminho}."
                    .replace(",", "."))


def main():
    p = argparse.ArgumentParser(description="Interface de consulta dos Estabelecimentos.")
    p.add_argument("--db", help="banco SQLite (padrao: ultimo .db em dados/)")
    args = p.parse_args()

    raiz = tk.Tk()
    Interface(raiz, Path(args.db) if args.db else banco_padrao())
    raiz.mainloop()


if __name__ == "__main__":
    main()
