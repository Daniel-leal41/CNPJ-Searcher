# AGENTS.md

Pipeline de dados publicos de CNPJ da Receita Federal. Python puro (stdlib +
Tkinter), sem `pyproject`/`requirements`, sem git. Rode sempre da raiz do repo.

## Comandos

```bash
python -m unittest discover -v        # suite completa, sem rede
python -m unittest tests.test_carregar -v
python -m unittest tests.test_carregar.TestCarregarEstabelecimentos.test_junta_shards_em_uma_tabela -v

python iniciar.py                     # assistente: baixa, monta e abre a GUI
python iniciar.py --verificar         # so mostra o estado (sem GUI)
python -m cnpjs.baixar                          # mes mais recente -> dados/AAAA-MM/
python -m cnpjs.baixar --arquivo Cnaes.zip
python -m cnpjs.inspecionar --arquivo Estabelecimentos0.zip --limite 2
python -m cnpjs.carregar --pasta dados/2026-10 --arquivo Estabelecimentos0.zip \
    --db dados/teste-estabelecimentos0.db --recriar
python -m cnpjs.carregar --pasta dados/2026-10 \
    --db dados/cnpj-2026-10.db --sem-indices     # todos os 10 shards, sem indices
python -m cnpjs.carregar --preparar --pasta dados/2026-10 \
    --db dados/cnpj-2026-10.db                   # cria os indices (retomavel)
python -m cnpjs.interface --db dados/cnpj-2026-10.db
```

## Arquitetura

- `iniciar.py`: assistente GUI de primeira execucao. Baixa so
  `Estabelecimentos*.zip`, `Municipios.zip` e `Cnaes.zip`, confere os zips,
  extrai o CNAE para `CNAE.CNAECSV`, chama `carregar` sem indices e depois
  `preparar`, grava `dados/cnpj-<mes>.json` e abre a interface em subprocesso.
  Se o JSON nao bate com os zips locais, renomeia o banco para `.bak-*` e
  recria (o backup e apagado no sucesso).
  Se o servidor tiver um mes mais novo que o ultimo local (`novo_mes`), avisa
  na GUI e abre `DialogoNovoMes`: pergunta antes de baixar, oferece continuar
  no mes antigo (`avaliar(mes_preferido=...)`) e tem uma caixa **desmarcada**
  para apagar os meses anteriores. A exclusao (`apagar_mes`) ocorre somente
  depois que o banco novo esta pronto; falha no download/carga preserva tudo.
  `meses_locais` conta pasta `AAAA-MM/` **ou** `cnpj-AAAA-MM.db`; `avaliar` e
  `executar` seguem com o banco completo mesmo sem os zips (`Continuar com`).
- `cnpjs/baixar.py`: Nextcloud publico via WebDAV (`PROPFIND` + `GET` em
  `/public.php/dav/files/<TOKEN>/...`). `--mes latest` ordena pastas
  `AAAA-MM`; baixa o que falta comparando tamanho; retoma `.part` com `Range`.
  `_listar_dav` repete o `PROPFIND` quando vem resposta vazia/XML invalido (o
  servidor as vezes faz isso) e `baixar_item` baixa usando o `href` ja listado,
  sem refazer o `PROPFIND` por arquivo; `iniciar.py` usa `baixar_item`.
- `cnpjs/inspecionar.py`: le so os primeiros 256 KB de cada zip remoto e
  descomprime em memoria. Importa o modulo (`from . import baixar`) e usa
  `baixar.HOST` em tempo de chamada; nao troque por `from .baixar import HOST`.
- `cnpjs/carregar.py`: le o CSV **direto do zip** (nunca extrai), junta
  `Estabelecimentos*.zip` numa tabela e cria um `.db` por mes. `--arquivo`
  limita a um shard; ordenacao natural aceita `Estabelecimentos10`. Tambem
  cria a tabela `municipios` a partir de `Municipios.zip` e coleta os pares
  `(uf, municipio)` durante a carga. `--sem-indices` pula so os indices (a
  tabela `municipios` e criada mesmo assim). `--preparar` completa um banco
  existente sem apagar nada (idempotente) e remove os indices antigos
  `idx_estab_uf`/`idx_estab_email`.
- `cnpjs/interface.py`: Tkinter somente leitura (`mode=ro`), paginacao de 100,
  pega o `.db` mais recente de `dados/` por padrao. Situacao padrao = `02`
  (Ativa). O filtro de nome e **prefixo** (`LIKE 'texto%'`), nao substring;
  `%texto%` faz full scan. Exporta o filtro inteiro em CSV `;`/UTF-8 BOM
  (teto de 200 mil linhas) e deixa escolher as colunas exibidas/exportadas.
- `pesquisar_csv.py` (raiz) e legado/independente: procura `CNAE.CNAECSV` ao
  lado dele; nao mova para `cnpjs/` sem ajustar esse caminho.

## Dados (nao assuma)

- CSVs sem cabecalho, separador `;`, CRLF e encoding **latin-1** (ISO-8859-1).
  Ha byte `0x8F`, que nao existe em cp1252; decodificar como cp1252 quebra.
- Estabelecimentos tem 30 colunas: `cnpj_basico;ordem;dv;...`. O CNPJ completo
  e a concatenacao dos 3 primeiros; as 2 ultimas sao situacao/data especial
  (nao existe FAX; a sequencia final e `DDD_3;TELEFONE_3;EMAIL`).
- Estabelecimentos nao tem razao social; a interface filtra por
  `nome_fantasia` (vazio em parte). Razao social exigiria carregar Empresas e
  juntar por `cnpj_basico`.
- CNPJ e codigos devem ficar `TEXT` (zeros a esquerda); vazio vira `NULL`;
  datas ficam `AAAAMMDD`; capital social (arquivo Empresas) vem com virgula
  decimal.
- Um mes tem ~7,3 GB compactado; descomprimido, todos os arquivos somam ~29 GB
  e so os Estabelecimentos ~17 GB (73.809.379 linhas). `dados/cnpj-2026-10.db`
  (10 shards) tem ~29,1 GB com indices; `Estabelecimentos0` sozinho tem
  31.028.464 linhas e ~13,6 GB.
- Indices em `carregar.py` cobrem `(cnae,situacao)`, `situacao`,
  `(uf,municipio,situacao)`, `(uf,situacao)`, `cnpj_basico`,
  `(data_inicio_atividade,situacao)`, `nome_fantasia COLLATE NOCASE` e
  parciais de e-mail/telefone. Listar/filtrar e rapido (ms); `count(*)` e a
  parte cara: ~0,35 s para ativas no shard de 31M, ~0,8 s no banco completo
  de 73,8M; e-mail+telefone juntos chegam a ~1,8 s. Filtros
  `LIKE '%...%'` e `COALESCE` fora dos indices parciais podem levar segundos.
- `municipios` tem 5.572 codigos; o CSV `Municipios.zip` traz `codigo;nome`
  (sem UF). A UF vem dos proprios estabelecimentos; por isso `--preparar`
  precisa da tabela `estabelecimentos` ja carregada.
- `CNAE.CNAECSV` e gerado/atualizado por `iniciar.py` a partir de
  `Cnaes.zip`; a interface tambem aceita `CSVs/CNAE.CNAECSV` como fallback.
  Nao versione o arquivo gerado.

## Armadilhas

- `carregar` apaga o `.db` em qualquer excecao (inclusive Ctrl-C); cargas
  interrompidas precisam de `--recriar`. Ele tambem exige `--recriar` se o
  banco ja existe. `--preparar` e o caminho seguro para um banco existente:
  nao apaga e pode ser repetido.
- Sem `--pasta`, o carregador usa a pasta de mes mais nova em `dados/`; sem
  `--db`, grava `dados/cnpj-<mes>.db`. Sem `--arquivo`, carrega **todos** os
  `Estabelecimentos*.zip` da pasta.
- Sem `Municipios.zip` na pasta, nao ha tabela `municipios`; a interface
  desabilita o filtro de municipio e mostra o codigo. Rode `--preparar` com a
  pasta que tem o arquivo.
- `apagar_mes` so aceita `AAAA-MM` (protege contra `..`) e apaga a pasta do
  mes, o `cnpj-<mes>.db*` e o `cnpj-<mes>.json*`. Nunca chame antes do sucesso
  da carga: e a rede de seguranca para nao perder o mes antigo.
- Os indices parciais de e-mail/telefone casam com as expressoes exatas
  `email IS NOT NULL` e `COALESCE(telefone1, telefone2, telefone3) IS NOT NULL`;
  mudar o filtro sem mudar o indice perde a otimizacao.
- O filtro de nome usa `LIKE 'prefixo%'` e maiusculas (NOCASE do SQLite so
  cobre ASCII). Nao troque por `%texto%` nem por `LOWER()` sem medir: perde o
  indice e volta a varrer a tabela inteira.
- A paginacao da interface **nao** usa `ORDER BY e.cnpj`: isso obrigava o
  SQLite a ordenar todo o conjunto filtrado (no shard de 31M: 4 s em "ativa",
  38 s com e-mail+telefone). A ordem e a do indice do filtro e fica estavel
  enquanto o plano nao muda; nao "conserte" com ORDER BY.
- O `TOKEN` do compartilhamento fica em `cnpjs/baixar.py`; se os downloads
  falharem com 404/503, confira/atualize o token ali.
- `dados/` contem artefatos caros de regenerar: 7,3 GB de zips, o `.db` de
  teste (~13,6 GB) e o banco completo `cnpj-2026-10.db` (~29,1 GB). Nao apague
  nem rebaixe sem necessidade.
- O servidor entrega ~1,4 MB/s por conexao (Range funciona; paralelizar acelera),
  mas o modulo baixa sequencialmente de proposito.
- Testes nao devem tocar a rede: `tests/test_baixar.py` sobe um Nextcloud
  falso; `tests/test_carregar.py` e `tests/test_iniciar.py` usam zips
  sinteticos. Testes de Tkinter so importam o modulo; para smoke test use
  `Tk()` com `withdraw()`.
- Nao introduza dependencias externas sem combinar; o projeto inteiro e stdlib.
