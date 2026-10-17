# CapCut Light Renderer

Aplicativo Windows para renderizar cortes de vídeo de um projeto CapCut usando a CPU (`libx264`, preset `ultrafast`).

## Uso

Abra `CapCutLightRenderer.exe`, selecione a pasta do projeto que contém `draft_content.json` e clique em **Renderizar**. Por padrão, o MP4 é salvo nessa pasta. Mantenha os arquivos de mídia do projeto acessíveis nos caminhos registrados no rascunho.

O renderer processa os cortes de vídeo básicos. Ele não aplica efeitos, textos, sobreposições, transições ou outros elementos visuais da timeline do CapCut.

## Build Windows

Em Windows com Python 3.12 ou superior:

```powershell
py -m pip install -r requirements.txt
py build.py
```

O executável portátil será criado em `dist\CapCutLightRenderer.exe` e inclui o FFmpeg. O instalador com atalho e desinstalação é criado pelo workflow Windows do GitHub Actions; baixe `CapCutLightRenderer-Setup.exe` na seção **Actions** da execução, em **Artifacts**.