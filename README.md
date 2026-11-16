# Cnpjs-searcher

Download, banco SQLite e interface Tkinter para consultar os dados públicos de
CNPJ da Receita Federal. O ponto de entrada é o **`iniciar.py`**: ele baixa só o
que falta, confere os arquivos, monta o banco e abre a interface.

## Requisitos

- Python 3.10 ou mais novo.
- Tkinter (no Debian/Ubuntu: `sudo apt install python3-tk`).
- Cerca de **50 GB livres** em disco para o mês completo (zips + banco +
  índices).
- Internet na primeira execução; depois dá para usar offline.
- Nenhuma dependência externa de `pip` — veja o `requirements.txt`.

## Início rápido

```bash
git clone <URL_DO_REPOSITORIO>
cd Cnpjs-searcher
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # não instala nada; é só conferência
python iniciar.py
```

Na primeira execução o `iniciar.py`:

1. descobre o mês mais recente;
2. lista o que falta e mostra uma estimativa de tempo;
3. **pergunta antes de baixar** (dá tempo de pegar um café);
4. baixa somente `Estabelecimentos*.zip`, `Municipios.zip` e `Cnaes.zip`;
5. confere tamanho e abertura dos zips;
6. monta `dados/cnpj-AAAA-MM.db` e cria os índices;
7. abre a interface de consulta.

Se o banco do mês já estiver completo, ele não pergunta nada: abre a interface.
Se um shard novo aparecer no servidor, baixa o que falta e recria o banco.

## Quando a Receita publica um mês novo

Se o servidor tiver uma pasta `AAAA-MM` mais recente que a sua cópia local
(ex.: `2026-11` quando você tem `2026-10`), o `iniciar.py`:

1. avisa no log e abre uma janela **“Nova pasta na Receita Federal”**;
2. mostra a estimativa de download, carga e índices;
3. oferece **“Baixar e preparar”**, **“Continuar com 2026-10”** (abre o banco
   antigo sem baixar nada) e **“Sair”**;
4. tem uma caixa, **desmarcada por padrão**, para apagar os meses anteriores.
   A exclusão só acontece **depois que o novo banco estiver pronto**; se o
   download ou a carga falhar, os meses antigos continuam no disco.

Apagar os meses antigos libera espaço (o mês completo ocupa ~37 GB, somando
zips + banco). Se a caixa ficar desmarcada, os meses antigos são preservados.

A detecção considera tanto as pastas `AAAA-MM/` quanto os bancos
`cnpj-AAAA-MM.db`. Se você apagou só os zips e manteve o banco, o mês antigo
continua aparecendo: dá para abri-lo em **“Continuar com AAAA-MM”** e, depois
do novo banco pronto, apagar o banco antigo pela caixa de seleção.

## O que fica no disco

| Caminho | Conteúdo |
|---|---|
| `dados/AAAA-MM/` | zips baixados |
| `dados/cnpj-AAAA-MM.db` | banco SQLite com os índices |
| `dados/cnpj-AAAA-MM.json` | estado da preparação (arquivos e linhas) |

O mês de referência (2026-10) tem **73.809.379 estabelecimentos** e ~29 GB de
banco com índices. O `iniciar.py` também atualiza o `CNAE.CNAECSV` na raiz a
partir do `Cnaes.zip`. `dados/`, `CSVs/` e o `CNAE.CNAECSV` gerado estão no
`.gitignore`; não versione os dados.

## Interface

- UF/município, CNAE principal (seleção múltipla pesquisável pela descrição),
  situação (padrão **ativa**), nome fantasia (prefixo), matriz/filial,
  intervalo de início de atividade, com e-mail e com telefone.
- Resultados paginados; exportação do filtro inteiro em CSV `;`/UTF-8 BOM, com
  teto de 200 mil linhas, usando as colunas escolhidas em **Colunas...**.
- As descrições de CNAE são extraídas do `Cnaes.zip` para `CNAE.CNAECSV` na
  raiz. Se já existir um `CNAE.CNAECSV` mais novo que o zip, ele é preservado.
  O fallback é `CSVs/CNAE.CNAECSV`.

O arquivo de Estabelecimentos não traz razão social; o filtro de nome usa
`nome_fantasia` (vazio em parte dos registros). Para incluir razão social seria
preciso carregar também os zips de Empresas.

## Comandos avançados

```bash
python iniciar.py --verificar               # mostra o estado, sem baixar nem abrir
python -m cnpjs.baixar                       # baixa todos os arquivos do mês
python -m cnpjs.baixar --arquivo Cnaes.zip
python -m cnpjs.inspecionar --arquivo Estabelecimentos0.zip --limite 2

python -m cnpjs.carregar --pasta dados/2026-10 \
    --db dados/cnpj-2026-10.db --sem-indices
python -m cnpjs.carregar --preparar --pasta dados/2026-10 \
    --db dados/cnpj-2026-10.db

python -m cnpjs.interface --db dados/cnpj-2026-10.db
```

Para recriar um banco do zero:

```bash
python -m cnpjs.carregar --pasta dados/2026-10 \
    --db dados/cnpj-2026-10.db --recriar
```

## Estrutura

| Caminho | Função |
|---|---|
| `iniciar.py` | assistente de primeira execução (GUI) |
| `cnpjs/baixar.py` | download via WebDAV no Nextcloud público |
| `cnpjs/inspecionar.py` | primeiras linhas dos CSVs sem baixar tudo |
| `cnpjs/carregar.py` | CSV direto do zip para SQLite, `municipios` e índices |
| `cnpjs/interface.py` | interface Tkinter de consulta |
| `tests/` | testes automatizados, sem rede |
| `CNAE.CNAECSV` | descrições de CNAE (gerado a partir de `Cnaes.zip`; fallback em `CSVs/`) |
| `pesquisar_csv.py`, `CSVs/` | app legado de pesquisa em CSVs (opcional) |

## Testes

```bash
python -m unittest discover -v
```

A suíte não toca a rede: o downloader é testado contra um Nextcloud falso e o
carregador usa zips sintéticos.

## Solução de problemas

- **Tkinter não encontrado** → `sudo apt install python3-tk` (Debian/Ubuntu).
- **404/503 no download** → o token público pode ter expirado; confira
  `TOKEN` em `cnpjs/baixar.py`.
- **Download interrompido** → rode `python iniciar.py` novamente; o `.part` é
  retomado com `Range`.
- **“resposta vazia” / `ParseError` ao listar** → o servidor devolveu um
  `PROPFIND` vazio. O `iniciar.py` tenta de novo automaticamente e, se
  persistir, mostra “nao consegui listar …”. Confira internet/proxy e rode de
  novo; o download reutiliza a listagem e não repete o `PROPFIND` por arquivo.
- **Banco sem índices ou sem municípios** → rode `--preparar` com a pasta e o
  banco corretos.
- **Banco desatualizado / shard novo** → o `iniciar.py` detecta e recria; ou
  apague `dados/cnpj-AAAA-MM.db` e `dados/cnpj-AAAA-MM.json` e rode de novo.
- **Quer voltar para o mês anterior** → na janela de pasta nova escolha
  **“Continuar com AAAA-MM”**; nada é baixado nem apagado. Isso funciona mesmo
  que os zips do mês antigo tenham sido apagados, desde que o `.db` exista.
- **Apagou só a pasta de zips** → o banco `cnpj-AAAA-MM.db` continua sendo
  reconhecido como um mês local; a interface abre normalmente.
- **Apagou o mês errado?** → os dados são públicos e o `iniciar.py` baixa de
  novo; a exclusão nunca toca no mês que está sendo preparado.
- **Pouco espaço** → o mês completo pede ~50 GB; o `iniciar.py` avisa antes.
- **A interface não abre** → rode
  `python -m cnpjs.interface --db dados/cnpj-AAAA-MM.db` no terminal para ver o
  erro.

## Fonte dos dados

Receita Federal — dados públicos de CNPJ, compartilhados no Nextcloud público.
O `TOKEN` é o do compartilhamento e pode mudar; se os downloads falharem,
confira `cnpjs/baixar.py`.

## Licença

MIT — veja `LICENSE`.
