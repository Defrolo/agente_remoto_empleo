---
description: Cambia el estado de una oferta (ej. /estado a1b2c3 aplicada enviada el lunes)
agent: job-hunter
---
Interpreta "$ARGUMENTS" como: <id> <estado> [nota].
Estados válidos: nueva, interesante, aplicada, entrevista, oferta, rechazada, descartada.
Ejecuta `python scripts/jobs.py status <id> <estado> --notes "<nota>"` (sin --notes si no hay nota)
y después `python scripts/jobs.py report`. Confirma el cambio en una línea. No hagas fetch.
