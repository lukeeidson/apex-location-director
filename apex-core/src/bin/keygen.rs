//! License key tool for Apex Location Director.
//!
//!   apex-keygen genkeypair
//!       Create a new signing keypair. Paste PUBLIC into license.rs
//!       (PUBLIC_KEY_B64) and keep PRIVATE secret — it mints every key.
//!
//!   apex-keygen make <id> <private_b64>
//!       Print a license key for <id> (any label: a name, order number,
//!       email, etc.). Give the printed key to that customer.

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD as B64};
use ed25519_dalek::{Signer, SigningKey};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("genkeypair") => {
            let mut seed = [0u8; 32];
            getrandom::getrandom(&mut seed).expect("OS randomness");
            let sk = SigningKey::from_bytes(&seed);
            println!("PRIVATE (keep secret, never ship): {}", B64.encode(seed));
            println!("PUBLIC  (paste into license.rs):   {}", B64.encode(sk.verifying_key().to_bytes()));
        }
        Some("make") => {
            let (Some(id), Some(priv_b64)) = (args.get(1), args.get(2)) else {
                eprintln!("usage: apex-keygen make <id> <private_b64>");
                std::process::exit(2);
            };
            let seed: [u8; 32] = B64
                .decode(priv_b64)
                .ok()
                .and_then(|b| b.try_into().ok())
                .expect("private key must be a base64url 32-byte seed");
            let sk = SigningKey::from_bytes(&seed);
            let sig = sk.sign(id.as_bytes());
            println!("{id}.{}", B64.encode(sig.to_bytes()));
        }
        _ => eprintln!("usage: apex-keygen genkeypair | make <id> <private_b64>"),
    }
}
