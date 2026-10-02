# Juego de pared con post-its

La webcam lee un tablero físico y el proyector dibuja una pelota encima. Un
post-it amarillo es el inicio, uno naranja es la llegada, los verdes son
rebotes y **cuatro** fucsia o violetas marcan las esquinas del campo. Esta
versión relee los post-its **entre intentos**.

## Montaje

1. Usá una pared plana. Colocá los cuatro post-its fucsia cerca de las cuatro
   esquinas del área proyectada. La webcam debe verlos a todos y el proyector
   debe iluminar todo el campo.
2. Poné un amarillo, un naranja y los verdes dentro de ese rectángulo. Dejá
   espacio entre papeles y una separación de al menos el radio de la pelota
   respecto de los bordes.
3. Fijá webcam y proyector. Usá iluminación ambiente estable y configurá el
   proyector como pantalla extendida de macOS. Si se mueve cualquiera de los
   dos, pulsá `C` para recalibrar.
4. El ordenador debe tener Python 3.12 y `uv`. En esta máquina ambos están
   disponibles. Desde esta carpeta:

   ```sh
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ./.venv/bin/python main.py
   ```

   Por defecto se abre en la segunda pantalla (índice `1`) si existe y usa
   la webcam `0`. En este Mac, AVFoundation enumera la cámara FaceTime como
   `0` y la Logitech HD Pro Webcam C920 como `1`. Para usar la C920, abrí
   **Terminal** y ejecutá:

   ```sh
   cd /Users/tobiasarraiza/Documents/Codex/2026-10-02/bien/outputs/postit-wall-game
   ./.venv/bin/python main.py --camera 1
   ```

   Cuando macOS solicite acceso a la cámara para Terminal, elegí **Permitir**.
   Si no aparece el aviso, revisá Ajustes del Sistema → Privacidad y seguridad
   → Cámara y activá Terminal si figura allí. El permiso corresponde a la
   aplicación que inicia Python.

## Calibración y partida

- `A` calibra automáticamente: el proyector muestra puntos blancos de
  coordenadas conocidas, la webcam los localiza y se calcula una homografía.
  Otros cuatro puntos verifican el error. Si la webcam no ve bastantes puntos,
  el programa ofrece calibración manual.
- `M` calibra manualmente: usá las flechas para mover una cruz hasta el centro
  de cada post-it fucsia, en el orden indicado. `Shift` reduce la velocidad y
  `Enter` confirma cada cruz.
- Antes de jugar, revisá que los contornos proyectados coincidan con los
  post-its y que la cuadrícula esté alineada. `Enter` acepta; `A` o `M` repite
  la calibración.
- `←` y `→` cambian la dirección de lanzamiento. `Espacio` lanza la pelota.
  La pelota rebota en los verdes, gana al tocar el naranja y pierde al salir
  del campo. `R` reinicia con el mismo tablero.
- Para **mover post-its entre intentos**, detené el intento y pulsá `S`. La
  proyección queda negra brevemente, se vuelve a leer el tablero y aparece una
  pelota nueva en el amarillo. `C` recalibra si se movió el equipo. `Esc`
  cierra el juego.

La calibración automática requiere que los puntos blancos proyectados sean
visibles para la webcam. Con luz muy intensa, sombras o exposición automática
variable, usá `M`. La calibración supone pared plana; una lente con mucha
distorsión puede necesitar corrección óptica antes de mapear puntos.

## Pruebas y evidencia

Las pruebas son sintéticas y se ejecutan sin hardware:

```sh
./scripts/verify.sh
```

El script ejecuta estas pruebas y evaluaciones:

```sh
./.venv/bin/python -m unittest discover -s . -p 'test_*.py' -q
./.venv/bin/python -m services.calibration.evals.synthetic
./.venv/bin/python -m services.vision.eval
./.venv/bin/python -m services.physics.evals.scenarios
./.venv/bin/python -m services.runtime.eval
```

La prueba física que falta hacer en la pared es revisar la cuadrícula sobre
papeles reales y confirmar que el error máximo mostrado en la calibración
  automática sea menor que la mitad del radio de la pelota. Cada calibración, lectura de
tablero e intento registra valores en `runs/events.jsonl` para poder revisar
errores de alineación, cantidad de rebotes y tiempo de llegada.

## Estructura

- `contracts/`: datos y coordenadas compartidos.
- `services/vision/`: segmentación de los post-its con el proyector en negro.
- `services/calibration/`: puntos proyectados y homografía cámara → proyector.
- `services/physics/`: pelota y colisiones en píxeles del proyector.
- `services/runtime/`: captura de cámara, pantalla, controles y registro.
- `main.py`: entrada del programa.

Cada servicio tiene sus propias pruebas, evaluación y explicación. El hook de
Git ejecuta las pruebas locales antes de cada commit; si clonás el proyecto,
activá el hook con `git config core.hooksPath .githooks`.
