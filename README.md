# CapCut Light Renderer

Aplicativo Windows para renderizar a timeline de um projeto CapCut usando a CPU (`libx264`, preset `ultrafast`).

## Uso

Abra `CapCutLightRenderer.exe`, selecione a pasta do projeto que contém `draft_content.json` e clique em **Renderizar**. Por padrão, o MP4 é salvo nessa pasta. Mantenha os arquivos de mídia do projeto acessíveis nos caminhos registrados no rascunho.

O renderer lê as trilhas e posições do próprio `draft_content.json`, combina vídeos e imagens no canvas do projeto e sincroniza as faixas de áudio. Os arquivos da pasta `Resources` precisam continuar acessíveis. Efeitos, textos, máscaras, animações e transições específicas do CapCut ainda podem não ser reproduzidos; valide cada projeto antes de usar o resultado final.

## Build Windows

Em Windows com Python 3.12 ou superior:

```powershell
py -m pip install -r requirements.txt
py build.py
```

O executável portátil será criado em `dist\CapCutLightRenderer.exe` e inclui o FFmpeg. O instalador com atalho e desinstalação é criado pelo workflow Windows do GitHub Actions; baixe `CapCutLightRenderer-Setup.exe` na seção **Actions** da execução, em **Artifacts**.