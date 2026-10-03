# Servicio UVC: control de la cámara por USB

Chrome en macOS no le da a la página el modo manual de balance de blancos,
exposición y foco de algunas cámaras; con la Logitech C920 no ofrece ninguno.
La cámara sí los acepta directamente por USB, incluso mientras Chrome la está
usando. Este servicio se los manda desde el servidor.

## Piezas

| Archivo | Qué hace |
| --- | --- |
| `native/uvc_control.c` | Programa en C que habla con la cámara por IOKit (`IOUSBHostInterface`) con pedidos UVC `GET_*` y `SET_CUR`. Salida: un objeto JSON. |
| `__init__.py` | `build()` lo compila con `clang` la primera vez o cuando cambia la fuente, en `build/` (fuera de git). `run()` lo ejecuta y convierte errores en `UvcError`. `UvcBridge` serializa las llamadas y habla el vocabulario del panel. |
| `mapping.py` | Traducción pura entre controles UVC y los nombres de Chrome (`whiteBalanceMode`, `colorTemperature`, `exposureMode`, `exposureTime`, `focusMode`, `focusDistance`, `brightness`, `contrast`, `saturation`, `sharpness`, `gain`). |
| `smoke.py` | Prueba con la cámara real; solo lectura salvo `--write-test`. |

```sh
services/uvc/build/uvc-control list
services/uvc/build/uvc-control get 046d 082d
services/uvc/build/uvc-control set 046d 082d wb_auto=0 wb_temperature=4000
```

## Cómo encuentra los controles

Las unidades salen del descriptor de configuración de la propia cámara: la
terminal de cámara (`wTerminalType 0x0201`) y la unidad de procesamiento. En
la C920 son la 1 y la 3. Si el descriptor no se puede leer, prueba ids del 1
al 15, pero solo acepta una unidad cuyos valores tengan sentido: el autofoco
tiene que ser 0 o 1, y el balance automático 0 o 1 junto con una temperatura
en kelvin. Esa verificación hace falta porque los selectores se repiten entre
tipos de unidad: el zoom de la terminal usa el mismo número que el balance
automático.

## Reglas al escribir

`mapping.plan()` arma la lista de escrituras con estas reglas:

1. Primero apaga los automáticos: `wb_auto=0`, `ae_mode=1` (manual) y
   `focus_auto=0`.
2. Después escribe los valores, ajustados al rango y al paso de la cámara.
3. Un valor cuyo automático sigue encendido se saltea, porque la cámara lo
   rechaza.
4. Un control que la cámara no tiene también se saltea.

`ae_mode` usa el mapa de bits de UVC: 1 es manual, 8 es prioridad de apertura
(el automático de la C920). Para el panel, cualquier modo distinto de 1 es
`continuous`.

## Pruebas

- **Gate, sin cámara.** `tests/test_mapping.py` usa una lectura real de la
  C920 de esta Mac (`tests/c920_raw.json`). `tests/test_client.py` usa
  programas falsos lanzados con `/bin/sh`: un ejecutable recién creado tarda
  ~350 ms en su primera ejecución mientras macOS lo revisa, y un script pasado
  a `/bin/sh` no.
- **Periódica.** `python -m services.uvc.smoke`, en `verify.sh`, compila,
  busca la cámara y lee los controles. Sin cámara USB responde "skipped" y no
  falla.

Verificado con la C920 real en macOS 26.5 y Chrome 154, con la cámara en uso:

- la lectura devolvió los 11 controles con sus rangos;
- `wb_auto` pasó de 1 a 0, se mantuvo al releer y volvió a 1.
