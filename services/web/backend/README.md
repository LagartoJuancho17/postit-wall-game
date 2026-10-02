# Backend web local

El navegador es dueño de la webcam (`getUserMedia`) y del lienzo del
proyector. Python recibe una foto JPEG solo cuando se relee el tablero **entre
intentos**. Esto permite que macOS use el permiso de cámara de Chrome, que ya
está habilitado, y evita que el proceso de Python abra la cámara.

El servidor escucha únicamente en `127.0.0.1`. `create_server(port=0)` devuelve
un `ThreadingHTTPServer` sin iniciar; `serve(port=8765)` lo inicia hasta que se
cierre el proceso. Para pruebas sin sockets, `create_server(port=8765,
bind=False)` crea el handler sin llamar a `bind`.

## Contrato HTTP

| Método y ruta | Uso |
| --- | --- |
| `GET /` | Panel de cámara y controles. |
| `GET /projector` | Lienzo emergente del proyector. |
| `GET /api/state` | Estado actual, geometría en píxeles de cámara y proyector. |
| `GET /api/tick` | Avance por reloj monotónico de la física a pasos de 1/120 s. |
| `POST /api/frame` | Bytes `image/jpeg` de una captura oscura; primera lectura o relectura. |
| `POST /api/action` | Objeto JSON con una acción de la tabla siguiente. |

Las escrituras requieren `X-Postit-Client: 1`. Si se envía `Origin`, debe ser
exactamente `http://127.0.0.1:<puerto>`. También se valida `Host` en todas las
rutas para evitar rebinding. El cuerpo HTTP tiene un límite de 8 MB y el JPEG
un límite de 12 megapíxeles, comprobado **antes** de decodificarlo.

| `action` | Datos | Efecto |
| --- | --- | --- |
| `projection_open` | `width`, `height` en píxeles reales del canvas | Abre o redimensiona; al cambiar tamaño invalida la calibración. |
| `projection_close` | ninguno | Oscurece y pausa el intento activo. |
| `camera_changed` | ninguno | Invalida capturas, calibración y tablero tras cambiar de cámara. |
| `projection_black` | ninguno | Prepara una lectura oscura. |
| `calibrate` | ninguno | Empieza la alineación manual de cuatro cruces con los fucsias. |
| `calibration_move` | `x`, `y` | Mueve la cruz activa. |
| `calibration_confirm` | `x`, `y` opcionales | Guarda la cruz; la cuarta calcula la homografía y prepara el tablero. |
| `aim` | `degrees` | Dirección del próximo lanzamiento. |
| `aim_left`, `aim_right` | ninguno | Ajusta la dirección 5°. |
| `launch` | ninguno | Inicia un intento. |
| `reset` | ninguno | Crea una pelota nueva sin releer papeles. |
| `pause`, `resume` | ninguno | Pausa o reanuda el intento. |

Antes de `/api/frame`, la interfaz envía `projection_black`, espera la
confirmación de que la ventana pintó negro y deja 150 ms para que el proyector
y la webcam se estabilicen. El backend exige que la ventana esté abierta y el
modo sea `black`. La primera captura produce `camera_board`, con coordenadas
de cámara. Tras cuatro puntos manuales, `board` contiene la misma geometría
en píxeles del proyector. Las capturas posteriores aplican esa misma matriz y
preparan una pelota nueva. Si se mueve la webcam o el proyector, hay que
calibrar otra vez. Una captura fallida conserva el tablero anterior.

`GET /api/state` incluye `phase`, `message`, `camera` (dimensiones de la última
captura), `camera_board`, `board`, `calibration`, `game` y `projection`. Cada
polígono se codifica como `{ "points": [{ "x": ..., "y": ... }, ...] }`.
`projection.target` incluye `x`, `y`, `index` (1 a 4) y `label` durante la
calibración. `width` y `height` son los píxeles CSS lógicos de
`window.innerWidth` y `window.innerHeight`; el popup puede multiplicar el
respaldo del canvas por `devicePixelRatio`, siempre que el contexto se escale
por el mismo valor. Las coordenadas de la homografía siguen siendo píxeles
CSS lógicos. La física usa el reloj monotónico del servidor, por
lo que dos clientes consultando `/api/tick` no aceleran la pelota. Un hueco de
más de 250 ms pausa ese intervalo, como hace el modo Pygame.

## Evidencia

`runs/web-events.jsonl` guarda eventos de tamaño del proyector, capturas,
calibración y resultado de intentos. La evaluación sintética comprueba error
de mapeo menor de 1 px, victoria, relectura de un rebote movido y pérdida de
calibración al redimensionar:

```sh
./.venv/bin/python -m services.web.backend.eval
```

Las pruebas HTTP usan handlers sin socket ni hardware. La prueba pendiente en
la pared es verificar visualmente los contornos después de colocar webcam y
proyector fijos. Con solo cuatro cruces, la homografía ajusta los puntos
seleccionados pero no estima el error en puntos de control independientes.
