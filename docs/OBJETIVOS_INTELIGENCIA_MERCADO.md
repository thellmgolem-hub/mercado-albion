# Objetivos da Central de Inteligencia de Mercado

Este documento registra a direcao discutida com o usuario para orientar
proximos agentes e evitar que a plataforma volte a ser apenas um scanner de
flips.

## Objetivo maior

Construir uma plataforma local de inteligencia economica para Albion Online,
baseada em coleta rigorosa e longa de dados, capaz de estudar mercados como um
analista profissional estudaria uma microestrutura real: preco, volume,
liquidez, tendencia, anomalia, risco, profundidade, previsao e validade
historica das estrategias.

## Principios

- Separar observacao, analise e decisao.
- Nunca confundir cache operacional com memoria historica de longo prazo.
- Registrar qualidade da coleta: quando, cidade, fonte, cobertura e buracos.
- Preferir conclusoes probabilisticas e auditaveis a sinais magicos.
- Fazer backtesting antes de transformar padrao em diretriz operacional.
- Mostrar respostas simples para leigos, mas manter metricas tecnicas
  verificaveis para traders avancados.

## Dados que devem sustentar a plataforma

- Precos atuais e snapshots historicos.
- Historico agregado de preco medio e quantidade vendida.
- Ouro como serie macro de inflacao/deflacao.
- Watchlists monitoradas de forma disciplinada.
- Logs de coleta dos scouts.
- Livro de ordens completo quando NATS/dumps forem implementados.
- Metadados de item: peso, tier, encanto, categoria, receitas e bonus de cidade.

## Ferramentas analiticas desejadas

- Item Lab: pagina analitica por item/cidade/qualidade.
- Graficos de preco medio e volume, semelhantes ao mercado do jogo.
- Medias moveis, volatilidade, z-score, momentum e aceleracao.
- Deteccao de outliers e dados suspeitos.
- Heatmap intercidades de spread, ROI, volume e frescor.
- Backtesting de estrategias com taxas, volume e slippage.
- Predicao probabilistica com intervalos e confianca.
- Profundidade de livro: slippage, preco medio de execucao e liquidez acumulada.
- Score de decisao multifator: margem, ROI, volume, frescor, estabilidade e risco.

## Ordem cientifica recomendada

1. Fortalecer coleta auditavel e watchlists.
2. Criar score de qualidade dos dados.
3. Construir series derivadas limpas.
4. Implementar Item Lab com estatisticas e interpretacao.
5. Criar backtesting.
6. Implementar livro de ordens e profundidade.
7. Adicionar previsao probabilistica e alertas.

## Estado atual

A plataforma ja tem:

- `prices`, `history`, `gold`, `price_snapshots`.
- Recomendacoes de flips cache-only com volume, frequencia, frescor e score.
- Grafico historico de preco medio e volume.
- Botao nas recomendacoes para abrir o historico do item/rota.

Proximo nucleo: transformar a aba Historico em Item Lab com metricas
estatisticas, qualidade do dado, interpretacao automatica e sinais tecnicos.
