# PID-Autotune

Application Python/PyQt6 pour extraire la Blackbox d'un controleur de vol
Betaflight connecte en USB. Cette premiere version fournit l'interface et une
extraction MSP Dataflash vers un fichier `.bbl`.

## Installation

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Lancement

```powershell
python app\main.py
```

## Utilisation

1. Connecter le controleur de vol Betaflight en USB.
2. Cliquer sur `Rafraichir` si le port n'apparait pas.
3. Choisir le dossier et le nom du fichier `.bbl`.
4. Cliquer sur `Extraire la Blackbox`.

L'extraction utilise les commandes MSP `MSP_DATAFLASH_SUMMARY` et
`MSP_DATAFLASH_READ`. Elle doit etre testee sur le materiel cible avant
d'ajouter l'analyse et le reglage automatique des PID.
