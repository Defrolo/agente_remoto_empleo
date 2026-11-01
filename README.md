# Agente de búsqueda de trabajo remoto (OpenCode + GitHub Actions)

Un script en Python hace el trabajo mecánico (descargar, filtrar, deduplicar, guardar estado) y los
agentes de OpenCode hacen solo lo que necesita criterio (puntuar, verificar, redactar).

```
7 fuentes → filtros deterministas → SQLite → @job-scorer (Haiku) → @job-verifier (Sonnet, top)
         → @cover-letter (Sonnet, verificadas) → ofertas.md + aviso por Telegram
```

## Puesta en marcha (10 minutos)

1. **Edita `perfil.md`** (lo leen los agentes) y **`config.json`** (palabras clave y filtros del script).
2. **Comprueba los modelos:** `opencode models` y ajusta el campo `model:` de cada agente si el ID no coincide.
3. **Comprueba las fuentes:** `python scripts/jobs.py check`. Desactiva en `config.json` las que fallen.
4. **Pruébalo en local:** abre `opencode` en esta carpeta y escribe `/buscar`.
5. **Sube a un repo PRIVADO** (tiene datos personales):
   ```bash
   git init && git add . && git commit -m "agente trabajo remoto"
   gh repo create busqueda-trabajo --private --source=. --push
   gh secret set ANTHROPIC_API_KEY
   gh secret set TG_TOKEN     # opcional: bot de @BotFather
   gh secret set TG_CHAT      # opcional: tu chat id
   gh workflow run buscar.yml
   ```

## Uso diario

| Comando en OpenCode | Qué hace |
|---|---|
| `/buscar` o `/buscar rust, go` | Flujo completo (con palabras clave extra opcionales) |
| `/estado a1b2c3 aplicada enviada el lunes` | Actualiza el seguimiento de una oferta |
| `/resumen` | Estado del embudo y mejores ofertas, sin descargar nada |

Estados: `nueva, interesante, aplicada, entrevista, oferta, rechazada, descartada`.
Resultados en `ofertas.md`, cartas en `cartas/<id>.md`, estado en `data/jobs.db`.

## Qué lo hace robusto

- **Embudo barato:** ~90% de las ofertas se descartan por reglas antes de gastar un token.
- **Cada fuente falla por separado:** si una API cae, las demás siguen y el informe lo indica.
- **Sin duplicados:** por URL (sin parámetros de tracking) y por empresa+título entre portales.
- **Antiinyección:** las ofertas son texto de terceros. El script descarta las que intentan dar órdenes
  a una IA, y cada agente tiene instrucciones de tratarlas como datos. Además, `bash` solo permite
  `python scripts/jobs.py *`, y los evaluadores no pueden editar ni ejecutar nada.
- **Verificación en dos etapas:** solo las de 8+ se abren para comprobar que siguen activas, admiten
  España y qué contrato ofrecen.
- **Tests offline:** `python -m unittest discover -s tests` (también se ejecutan antes del agente en CI).

## Si algo no funciona

- **Permisos que no se aplican** (patrones de `bash` o `edit`): la sintaxis cambia entre versiones de
  OpenCode. Compáralo con opencode.ai/docs/agents y, como último recurso, pon `edit: allow`.
- **Una fuente devuelve 0 ofertas o error:** `python scripts/jobs.py check`. Los formatos de las APIs
  pueden cambiar; ajusta la función `src_*` correspondiente en `scripts/jobs.py` (OpenCode puede hacerlo
  por ti) o desactívala.
- **Pocas ofertas:** amplía `keywords`, sube `max_age_days` o vacía `exclude_title`. Mira los motivos con
  `python scripts/jobs.py stats`.
- **Demasiado ruido:** sube `min_score_to_save`, o añade `require_title_any` (p. ej. `["engineer","developer"]`).

## Términos de uso

RemoteOK pide enlazar a su sitio al mostrar sus ofertas; Remotive limita la frecuencia de consulta
(este proyecto hace una sola petición al día). Uso personal, sin reventa de datos.
