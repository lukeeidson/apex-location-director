//! Offline license-key verification.
//!
//! A license key is `<id>.<signature>`, where `signature` is an Ed25519
//! signature over `id` made with the private signing key (which only the
//! author holds — mint keys with the `apex-keygen` tool). The app embeds only
//! the PUBLIC key, so it can verify a key is genuine but cannot forge one.
//!
//! This stops casual "share the app for free" copying. It is not DRM: anyone
//! able to patch the binary can bypass any client-side check.

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD as B64};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};

/// Public verification key (base64url, 32 bytes). Replace with your own from
/// `apex-keygen genkeypair`. The matching private key never ships in the app.
pub const PUBLIC_KEY_B64: &str = "UVrVjz8lWv5doRWccAgerpFxA762RHTQ0oAGSuj-VPY";

/// True when `license` is a genuine key signed by the matching private key.
pub fn verify(license: &str) -> bool {
    let Some((id, sig_b64)) = license.trim().split_once('.') else {
        return false;
    };
    if id.is_empty() {
        return false;
    }
    let Some(key) = decode_key() else { return false };
    let Ok(sig_bytes) = B64.decode(sig_b64) else { return false };
    let Ok(sig_arr) = <[u8; 64]>::try_from(sig_bytes.as_slice()) else { return false };
    key.verify(id.as_bytes(), &Signature::from_bytes(&sig_arr)).is_ok()
}

fn decode_key() -> Option<VerifyingKey> {
    let bytes = B64.decode(PUBLIC_KEY_B64).ok()?;
    let arr = <[u8; 32]>::try_from(bytes.as_slice()).ok()?;
    VerifyingKey::from_bytes(&arr).ok()
}
