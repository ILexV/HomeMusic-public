//! Navidrome connection. Step 3 adds the HTTP calls. This module only reads the local config
//! and builds the Subsonic token the same way the Hub does: `t = md5(password + salt)`.

use std::path::PathBuf;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServerConfig {
    pub server: String,
    pub user: String,
    pub password: String,
    pub loaded: bool,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            server: "http://127.0.0.1:4533".into(),
            user: String::new(),
            password: String::new(),
            loaded: false,
        }
    }
}

impl ServerConfig {
    pub fn load() -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("config.toml");
        match std::fs::read_to_string(&path) {
            Ok(text) => parse_config(&text),
            Err(_) => Self::default(),
        }
    }

    /// Short status for the window. Never includes the password.
    pub fn status_line(&self) -> String {
        if !self.loaded {
            return "Navidrome: нет config.toml".into();
        }
        if self.user.is_empty() {
            return format!("Navidrome: в config.toml пустой логин ({})", self.server);
        }
        format!("Navidrome: ждёт подключения к {}", self.server)
    }
}

pub fn parse_config(text: &str) -> ServerConfig {
    let mut cfg = ServerConfig { loaded: true, ..ServerConfig::default() };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else { continue };
        let value = value.trim().trim_matches('"').to_string();
        match key.trim() {
            "server" => cfg.server = value,
            "user" => cfg.user = value,
            "password" => cfg.password = value,
            _ => {}
        }
    }
    cfg
}

/// Step 3 sends this as the Subsonic `t` parameter. Nothing calls it until then.
#[allow(dead_code)]
pub fn subsonic_token(password: &str, salt: &str) -> String {
    use md5::{Digest, Md5};
    let digest = Md5::digest(format!("{password}{salt}"));
    format!("{digest:x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_server_user_and_password() {
        let cfg = parse_config(
            "server = \"http://192.168.1.51:4533\"\nuser = \"lex\"\npassword = \"secret\"\n",
        );
        assert!(cfg.loaded);
        assert_eq!(cfg.server, "http://192.168.1.51:4533");
        assert_eq!(cfg.user, "lex");
        assert_eq!(cfg.password, "secret");
        assert!(!cfg.status_line().contains("secret"));
    }

    #[test]
    fn token_is_md5_of_password_plus_salt() {
        assert_eq!(subsonic_token("secret", "abcd"), "42d635acbe6c440ab8f6e5ca0859f45c");
    }
}
