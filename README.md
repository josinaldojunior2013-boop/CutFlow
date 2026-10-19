# CapCut Light Renderer

Aplicativo Windows para renderizar a timeline de um projeto CapCut usando a CPU (`libx264`, preset `ultrafast`).

## Uso

Abra `CapCutLightRenderer.exe` e selecione a pasta do projeto que contém `draft_content.json`. A timeline horizontal mostra a duração e posição das faixas, com tipos de mídia por cor e marcadores de efeitos/transições associados pelo draft. A tabela lista clipes, faixas, início, duração e disponibilidade das mídias. Use **Buscar pasta de mídia** para religar correspondências únicas por caminho/nome; selecione uma linha para corrigir casos ambíguos manualmente, ajustar início/duração/trim/volume ou ativar/ignorar a faixa inteira. A barra de progresso acompanha o tempo reportado pelo FFmpeg. Por padrão, o MP4 é salvo na pasta do projeto.

O renderer lê as trilhas e posições do próprio `draft_content.json`, combina vídeos e imagens no canvas do projeto e sincroniza as faixas de áudio. A renderização aceita até 2.000 clipes ativos por projeto; acima desse limite, ignore algumas faixas ou clipes antes de renderizar. Mídias ausentes em faixas ativas bloqueiam a renderização até serem religadas ou a faixa ser ignorada. Os arquivos da pasta `Resources` precisam continuar acessíveis. Efeitos, textos, máscaras, animações e transições específicas do CapCut ainda podem não ser reproduzidos; valide cada projeto antes de usar o resultado final.

Clipes sem caminho próprio na faixa **Panel Shadow** são associados à imagem da faixa de painéis quando ocupam o mesmo intervalo da timeline. Assim, recursos de sombra internos do CapCut não são tratados como arquivos externos ausentes; a aparência exata da sombra pode variar no vídeo renderizado.

## Converter projeto para Filmora 13

Abra a pasta do projeto CapCut e religue todas as mídias ausentes. Clique em **Exportar Filmora 13...** e salve o `.wfp`; depois abra esse arquivo no Filmora. O exportador usa o modelo nativo Filmora 13 incluído no aplicativo, mantendo as posições e durações dos clipes, as faixas de áudio e os cortes. As transições do CapCut são substituídas pela transição **Fade Grayscale** do modelo Filmora e podem ter aparência diferente. Efeitos de vídeo, máscaras e animações por keyframes que não existam no modelo Filmora não são transferidos; o aplicativo informa essas limitações ao terminar.

O `.wfp` referencia as mídias nos caminhos atuais (incluindo os caminhos religados); ele não copia os vídeos e áudios para dentro do projeto. Mantenha os arquivos acessíveis nesses caminhos ao abrir e renderizar no Filmora.

## Build Windows

Em Windows com Python 3.12 ou superior:

```powershell
py -m pip install -r requirements.txt
py build.py
```

O executável portátil será criado em `dist\CapCutLightRenderer.exe` e inclui o FFmpeg. O instalador com atalho e desinstalação é criado pelo workflow Windows do GitHub Actions; baixe `CapCutLightRenderer-Setup.exe` na seção **Actions** da execução, em **Artifacts**.