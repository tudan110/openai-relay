module.exports = {
  apps: [
    {
      name: "openai-relay",
      script: "app.py",
      interpreter: "python3",
      cwd: __dirname,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        RELAY_PORT: "55302",
        RELAY_ADMIN_KEY: "REPLACE_WITH_REAL_ADMIN_KEY",
        RELAY_LEGACY_KEY: "",
        RELAY_STRICT: "1",
        RELAY_PROXY_HOST: "127.0.0.1",
        RELAY_PROXY_PORT: "7897",
        OPENAI_UPSTREAM_AUTH_MODE: "codex_auth_file",
        OPENAI_UPSTREAM_HOST: "chatgpt.com",
        OPENAI_UPSTREAM_PREFIX: "/backend-api/codex",
        CODEX_AUTH_FILE: "/home/ubuntu/.codex/auth.json",
        RELAY_MODELS: "gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna,gpt-5.5,gpt-5.4,gpt-5.4-mini,gpt-5.3-codex-spark,codex-auto-review",
        TZ: "Asia/Shanghai"
      }
    }
  ]
}
