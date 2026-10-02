# Servicio web

Panel de control y ventana del proyector en el navegador, con un servidor
local en Python. El navegador abre la webcam y dibuja la proyección; Python
recibe una foto JPEG solo al escanear.

| Carpeta | Contenido |
| --- | --- |
| `backend/` | Servidor HTTP en `127.0.0.1`, estado del juego y contrato HTTP. Ver `backend/README.md`. |
| `ui/` | `index.html` + `app.js` (panel), `projector.html` + `projector.js` (ventana del proyector) y `ui-core.mjs`, la lógica pura que ambos comparten. |
| `contract/` | `state-matrix.json`: snapshot real de `/api/state` en cada fase y qué acciones acepta el backend allí. |
| `tests/` | Pruebas rápidas: `*.test.mjs` (Node) y `test_contract_matrix.py`. |
| `evals/` | `flows.mjs`: acuerdo entre los botones del panel y el backend. |

## Matriz de estados

`contract/matrix.py` lleva el `GameController` real a once fases (proyector
cerrado, esperando cámara, escaneado, calibrando, listo, en juego, en pausa
con y sin proyector, llegada, fuera, listo sin proyector). En cada fase prueba
las seis acciones del panel sobre un controlador recién construido y guarda
si el backend las acepta. El navegador decide sus botones solo con el
snapshot, así que las pruebas de JavaScript usan estos snapshots reales en
lugar de estados inventados.

```sh
./.venv/bin/python -m services.web.contract.matrix   # regenerar
```

`tests/test_contract_matrix.py` vuelve a construir la matriz y falla si la
guardada ya no coincide con el backend: aceptación de acciones, claves y tipos
del snapshot, y los valores de los que depende el panel (`phase`,
`game.status`, `game.paused`, `projection.open`, `projection.mode`,
`calibration.status`). Las coordenadas pueden variar sin que falle.

## Evaluación de flujos

```sh
node services/web/evals/flows.mjs
```

Umbral 1.0. Por fase comprueba que:

- **sound**: todo botón habilitado sea una acción que el backend acepta.
- **complete**: con el proyector abierto, toda acción que el backend acepta
  tenga su botón habilitado.
- **no_camera**: sin cámara, escanear, calibrar y lanzar estén deshabilitados.

Además recorre el camino del operador (escanear, calibrar, apuntar, lanzar,
reiniciar, continuar una pausa) y verifica que cada fase con el proyector
cerrado se recupere al reabrirlo con `J`. El informe incluye
`disagreements`: la cantidad de diferencias entre panel y backend.

Esta evaluación encontró el fallo de la pausa: al cerrar el proyector durante
un intento, el backend lo pausa y sigue informando `status: "running"` con
`paused: true`. El panel lo trataba como pelota en movimiento: ocultaba
**Continuar intento** y bloqueaba escanear y calibrar, que el backend sí
acepta. Antes del arreglo: puntuación 0.846 con 2 diferencias; después: 1.0 y
0.

## Pruebas

```sh
node --test services/web/tests/*.test.mjs
./.venv/bin/python -m unittest services.web.tests.test_contract_matrix
```

`popup-contract.test.mjs` revisa por texto el cableado que no se puede
ejecutar en Node: que `J` abra el proyector desde el gesto del usuario, el
negro antes de capturar, el cambio de cámara, el botón de continuar y que
cada `id` que buscan los scripts exista en su HTML.
