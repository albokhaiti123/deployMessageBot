module.exports = {
  apps: [
    {
      name: "deploy-msg-bot",
      script: "main.py",
      interpreter: "./venv/bin/python",
      // On Windows use: interpreter: "venv\\Scripts\\python.exe",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      max_memory_restart: "512M",
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      out_file: "logs/pm2_out.log",
      error_file: "logs/pm2_err.log",
      merge_logs: true,
    },
  ],
};
