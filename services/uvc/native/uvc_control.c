// uvc-control: read and set the controls of a USB (UVC) camera on macOS.
//
//   uvc-control list
//   uvc-control get <vendor-hex> <product-hex>
//   uvc-control set <vendor-hex> <product-hex> name=value [name=value ...]
//
// Prints exactly one JSON object on stdout. Exit 0 on success, 1 on a usage
// error, 2 on a device error. Writes go through the camera's control
// endpoint, which macOS allows while another app (Chrome) streams from it.
// "set" applies the pairs in the order given, then prints the "get" result.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { CAMERA_TERMINAL, PROCESSING_UNIT };
enum { GET_CUR = 0x81, GET_MIN = 0x82, GET_MAX = 0x83, GET_RES = 0x84, GET_INFO = 0x86, GET_DEF = 0x87, SET_CUR = 0x01 };

typedef struct {
  const char *name;
  int unit;      // CAMERA_TERMINAL or PROCESSING_UNIT
  int selector;  // UVC control selector
  int length;    // bytes
  int is_signed;
} Control;

static const Control CONTROLS[] = {
  {"ae_mode", CAMERA_TERMINAL, 0x02, 1, 0},
  {"exposure", CAMERA_TERMINAL, 0x04, 4, 0},  // 100 µs units
  {"focus", CAMERA_TERMINAL, 0x06, 2, 0},
  {"focus_auto", CAMERA_TERMINAL, 0x08, 1, 0},
  {"brightness", PROCESSING_UNIT, 0x02, 2, 1},
  {"contrast", PROCESSING_UNIT, 0x03, 2, 0},
  {"gain", PROCESSING_UNIT, 0x04, 2, 0},
  {"saturation", PROCESSING_UNIT, 0x07, 2, 0},
  {"sharpness", PROCESSING_UNIT, 0x08, 2, 0},
  {"wb_temperature", PROCESSING_UNIT, 0x0A, 2, 0},
  {"wb_auto", PROCESSING_UNIT, 0x0B, 1, 0},
};
static const int CONTROL_COUNT = sizeof(CONTROLS) / sizeof(CONTROLS[0]);

typedef struct {
  IOUSBInterfaceInterface300 **intf;
  int interface_number;
  int unit_id[2];  // camera terminal, processing unit
  int vendor, product, location;
  char name[128];
} Camera;

static int number_property(io_service_t service, const char *key) {
  CFStringRef cf_key = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
  CFTypeRef value = IORegistryEntrySearchCFProperty(service, kIOServicePlane, cf_key, NULL,
                                                    kIORegistryIterateRecursively | kIORegistryIterateParents);
  CFRelease(cf_key);
  int out = -1;
  if (value && CFGetTypeID(value) == CFNumberGetTypeID()) CFNumberGetValue(value, kCFNumberIntType, &out);
  if (value) CFRelease(value);
  return out;
}

static void string_property(io_service_t service, const char *key, char *out, size_t size) {
  out[0] = '\0';
  CFStringRef cf_key = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
  CFTypeRef value = IORegistryEntrySearchCFProperty(service, kIOServicePlane, cf_key, NULL,
                                                    kIORegistryIterateRecursively | kIORegistryIterateParents);
  CFRelease(cf_key);
  if (value && CFGetTypeID(value) == CFStringGetTypeID()) CFStringGetCString(value, out, size, kCFStringEncodingUTF8);
  if (value) CFRelease(value);
}

static void json_string(const char *text) {
  putchar('"');
  for (const char *p = text; *p; p++) {
    if (*p == '"' || *p == '\\') printf("\\%c", *p);
    else if ((unsigned char)*p < 0x20) printf("\\u%04x", *p);
    else putchar(*p);
  }
  putchar('"');
}

static int fail(int code, const char *message, IOReturn kr) {
  printf("{\"error\": ");
  json_string(message);
  if (kr) printf(", \"code\": \"0x%08x\"", kr);
  printf("}\n");
  return code;
}

static IOReturn request(Camera *camera, int direction_in, int code, const Control *control,
                        unsigned char *data, int length) {
  IOUSBDevRequest req = {
    .bmRequestType = USBmakebmRequestType(direction_in ? kUSBIn : kUSBOut, kUSBClass, kUSBInterface),
    .bRequest = (UInt8)code,
    .wValue = (UInt16)(control->selector << 8),
    .wIndex = (UInt16)((camera->unit_id[control->unit] << 8) | camera->interface_number),
    .wLength = (UInt16)length,
    .pData = data,
  };
  return (*camera->intf)->ControlRequest(camera->intf, 0, &req);
}

static long decode(const Control *control, const unsigned char *data) {
  unsigned long raw = 0;
  for (int i = 0; i < control->length; i++) raw |= (unsigned long)data[i] << (8 * i);
  if (control->is_signed && control->length == 2) return (short)raw;
  return (long)raw;
}

// Unit ids from the camera's own configuration descriptor: the camera
// input terminal (wTerminalType 0x0201) and the processing unit of the
// VideoControl interface.
static int units_from_descriptor(io_service_t interface_service, Camera *camera) {
  io_service_t device = 0, node = interface_service;
  IOObjectRetain(node);
  while (node) {
    if (IOObjectConformsTo(node, "IOUSBHostDevice")) { device = node; break; }
    io_service_t parent = 0;
    kern_return_t kr = IORegistryEntryGetParentEntry(node, kIOServicePlane, &parent);
    IOObjectRelease(node);
    node = kr == KERN_SUCCESS ? parent : 0;
  }
  if (!device) return 0;
  IOCFPlugInInterface **plugin = NULL;
  SInt32 score;
  IOUSBDeviceInterface320 **dev = NULL;
  if (IOCreatePlugInInterfaceForService(device, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
                                        &plugin, &score) == kIOReturnSuccess && plugin) {
    (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID320), (LPVOID *)&dev);
    (*plugin)->Release(plugin);
  }
  IOObjectRelease(device);
  if (!dev) return 0;
  IOUSBConfigurationDescriptorPtr config = NULL;
  int found = 0;
  if ((*dev)->GetConfigurationDescriptorPtr(dev, 0, &config) == kIOReturnSuccess && config) {
    const unsigned char *bytes = (const unsigned char *)config;
    int total = USBToHostWord(config->wTotalLength), in_control = 0;
    for (int at = 0; at + 2 < total && bytes[at] >= 2; at += bytes[at]) {
      const unsigned char *d = bytes + at;
      if (d[1] == 0x04 && d[0] >= 7) {  // INTERFACE
        in_control = d[2] == camera->interface_number && d[5] == 14 && d[6] == 1;
      } else if (in_control && d[1] == 0x24 && d[0] >= 4) {  // CS_INTERFACE
        if (d[2] == 0x02 && d[0] >= 6 && (d[4] | d[5] << 8) == 0x0201) camera->unit_id[CAMERA_TERMINAL] = d[3];
        if (d[2] == 0x05) camera->unit_id[PROCESSING_UNIT] = d[3];
      }
    }
    found = camera->unit_id[CAMERA_TERMINAL] && camera->unit_id[PROCESSING_UNIT];
  }
  (*dev)->Release(dev);
  return found;
}

static int read_value(Camera *camera, int unit_kind, int unit, int selector, int length, long *out) {
  Control control = {"probe", unit_kind, selector, length, 0};
  int saved = camera->unit_id[unit_kind];
  unsigned char data[4] = {0};
  camera->unit_id[unit_kind] = unit;
  IOReturn kr = request(camera, 1, GET_CUR, &control, data, length);
  camera->unit_id[unit_kind] = saved;
  if (kr != kIOReturnSuccess) return 0;
  *out = decode(&control, data);
  return 1;
}

// Without a readable descriptor, probe ids 1..15. Selectors overlap
// between unit kinds (the terminal's zoom shares the white-balance-auto
// selector), so a unit only counts when its values look right: autofocus
// a boolean; white balance auto a boolean with a kelvin temperature.
static void units_by_probing(Camera *camera) {
  long a, b;
  for (int id = 1; id < 16 && !camera->unit_id[CAMERA_TERMINAL]; id++)
    if (read_value(camera, CAMERA_TERMINAL, id, 0x08, 1, &a) && (a == 0 || a == 1) &&
        read_value(camera, CAMERA_TERMINAL, id, 0x04, 4, &b) && b > 0)
      camera->unit_id[CAMERA_TERMINAL] = id;
  for (int id = 1; id < 16 && !camera->unit_id[PROCESSING_UNIT]; id++)
    if (id != camera->unit_id[CAMERA_TERMINAL] &&
        read_value(camera, PROCESSING_UNIT, id, 0x0B, 1, &a) && (a == 0 || a == 1) &&
        read_value(camera, PROCESSING_UNIT, id, 0x0A, 2, &b) && b >= 1500 && b <= 12000)
      camera->unit_id[PROCESSING_UNIT] = id;
}

static void find_units(io_service_t interface_service, Camera *camera) {
  camera->unit_id[CAMERA_TERMINAL] = 0;
  camera->unit_id[PROCESSING_UNIT] = 0;
  if (!units_from_descriptor(interface_service, camera)) {
    camera->unit_id[CAMERA_TERMINAL] = 0;
    camera->unit_id[PROCESSING_UNIT] = 0;
    units_by_probing(camera);
  }
}

static int open_camera(int vendor, int product, Camera *camera) {
  io_iterator_t iterator;
  if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOUSBHostInterface"), &iterator) != KERN_SUCCESS)
    return 0;
  io_service_t service;
  int found = 0;
  while (!found && (service = IOIteratorNext(iterator))) {
    if (number_property(service, "idVendor") == vendor && number_property(service, "idProduct") == product &&
        number_property(service, "bInterfaceClass") == 14 && number_property(service, "bInterfaceSubClass") == 1) {
      IOCFPlugInInterface **plugin = NULL;
      SInt32 score;
      if (IOCreatePlugInInterfaceForService(service, kIOUSBInterfaceUserClientTypeID, kIOCFPlugInInterfaceID,
                                            &plugin, &score) == kIOReturnSuccess && plugin) {
        camera->intf = NULL;
        (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID300), (LPVOID *)&camera->intf);
        (*plugin)->Release(plugin);
        if (camera->intf) {
          camera->interface_number = number_property(service, "bInterfaceNumber");
          camera->vendor = vendor;
          camera->product = product;
          camera->location = number_property(service, "locationID");
          string_property(service, "USB Product Name", camera->name, sizeof(camera->name));
          find_units(service, camera);
          found = 1;
        }
      }
    }
    IOObjectRelease(service);
  }
  IOObjectRelease(iterator);
  return found;
}

static void print_controls(Camera *camera) {
  printf("{\"device\": {\"vendor\": \"%04x\", \"product\": \"%04x\", \"location\": %d, \"name\": ",
         camera->vendor, camera->product, camera->location);
  json_string(camera->name);
  printf(", \"camera_terminal\": %d, \"processing_unit\": %d}, \"controls\": {",
         camera->unit_id[CAMERA_TERMINAL], camera->unit_id[PROCESSING_UNIT]);
  int first = 1;
  for (int i = 0; i < CONTROL_COUNT; i++) {
    const Control *control = &CONTROLS[i];
    if (!camera->unit_id[control->unit]) continue;
    unsigned char info = 0;
    if (request(camera, 1, GET_INFO, control, &info, 1) != kIOReturnSuccess || !(info & 1)) continue;
    const int codes[] = {GET_CUR, GET_MIN, GET_MAX, GET_RES, GET_DEF};
    const char *keys[] = {"cur", "min", "max", "res", "def"};
    printf("%s\"%s\": {\"settable\": %s", first ? "" : ", ", control->name, (info & 2) ? "true" : "false");
    first = 0;
    for (int k = 0; k < 5; k++) {
      unsigned char data[4] = {0};
      if (request(camera, 1, codes[k], control, data, control->length) == kIOReturnSuccess)
        printf(", \"%s\": %ld", keys[k], decode(control, data));
    }
    printf("}");
  }
  printf("}}\n");
}

static int list(void) {
  io_iterator_t iterator;
  if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOUSBHostInterface"), &iterator) != KERN_SUCCESS)
    return fail(2, "no se pudieron listar los dispositivos USB", 0);
  printf("{\"cameras\": [");
  io_service_t service;
  int first = 1;
  while ((service = IOIteratorNext(iterator))) {
    if (number_property(service, "bInterfaceClass") == 14 && number_property(service, "bInterfaceSubClass") == 1) {
      char name[128];
      string_property(service, "USB Product Name", name, sizeof(name));
      printf("%s{\"vendor\": \"%04x\", \"product\": \"%04x\", \"location\": %d, \"name\": ", first ? "" : ", ",
             number_property(service, "idVendor"), number_property(service, "idProduct"),
             number_property(service, "locationID"));
      json_string(name);
      printf("}");
      first = 0;
    }
    IOObjectRelease(service);
  }
  IOObjectRelease(iterator);
  printf("]}\n");
  return 0;
}

int main(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "list") == 0) return list();
  if (argc < 4 || (strcmp(argv[1], "get") != 0 && strcmp(argv[1], "set") != 0))
    return fail(1, "uso: uvc-control list | get <vendor> <product> | set <vendor> <product> nombre=valor...", 0);
  int vendor = (int)strtol(argv[2], NULL, 16);
  int product = (int)strtol(argv[3], NULL, 16);
  Camera camera;
  memset(&camera, 0, sizeof(camera));
  if (!open_camera(vendor, product, &camera)) return fail(2, "no se encontró la cámara USB pedida", 0);
  if (strcmp(argv[1], "set") == 0) {
    for (int a = 4; a < argc; a++) {
      char *equals = strchr(argv[a], '=');
      if (!equals) return fail(1, "cada ajuste tiene que ser nombre=valor", 0);
      *equals = '\0';
      const Control *control = NULL;
      for (int i = 0; i < CONTROL_COUNT; i++)
        if (strcmp(CONTROLS[i].name, argv[a]) == 0) control = &CONTROLS[i];
      if (!control) return fail(1, "control desconocido", 0);
      if (!camera.unit_id[control->unit]) return fail(2, "la cámara no tiene ese control", 0);
      char *end = NULL;
      long value = strtol(equals + 1, &end, 10);
      if (!end || *end) return fail(1, "el valor tiene que ser un entero", 0);
      unsigned char data[4];
      for (int i = 0; i < control->length; i++) data[i] = (unsigned char)((unsigned long)value >> (8 * i));
      IOReturn kr = request(&camera, 0, SET_CUR, control, data, control->length);
      if (kr != kIOReturnSuccess) {
        char message[96];
        snprintf(message, sizeof(message), "la cámara rechazó %s=%ld", control->name, value);
        (*camera.intf)->Release(camera.intf);
        return fail(2, message, kr);
      }
    }
  }
  print_controls(&camera);
  (*camera.intf)->Release(camera.intf);
  return 0;
}
