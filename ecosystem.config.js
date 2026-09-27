module.exports = {
  apps: [
    {
      name: "deploy-msg-bot",
      script: "main.py",
      interpreter: "python",
      // interpreter: "python3",  // uncomment on Linux/macOS
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,       // 5 seconds between restarts
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",   // ensures logs appear in real-time
      },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      out_file: "logs/pm2_out.log",
      error_file: "logs/pm2_err.log",
      merge_logs: true,
    },
  ],
};
