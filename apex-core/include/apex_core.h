// C interface to apex-core (src/ffi.rs). All calls block; run them off the
// main thread. Each returns NULL on success, or an error message to be freed
// with apex_free_string.
#pragma once
#include <stddef.h>

char *apex_set(const char *pairing_path, const char *ip, double latitude, double longitude);
char *apex_play(const char *pairing_path, const char *ip, const double *latitudes,
                const double *longitudes, size_t count, double speed_mps);
char *apex_clear(const char *pairing_path, const char *ip);
char *apex_mount(const char *pairing_path, const char *ip, const char *restore_dir);
void apex_free_string(char *s);

// Mac app: JSON request in, JSON response out, over usbmuxd (src/usb.rs).
char *apex_usb_call(const char *request_json);
