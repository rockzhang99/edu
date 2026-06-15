module.exports = {
  apps: [{
    name: 'edu',
    script: 'app.py',
    interpreter: 'python',
    cwd: '/root/edu',
    env: {
      FLASK_HOST: '0.0.0.0',
      FLASK_PORT: '5001',
      PYTHONUNBUFFERED: '1'
    },
    instances: 1,
    exec_mode: 'fork',
    watch: false,
    autorestart: true,
    restart_delay: 5000,
    max_restarts: 10,
    min_uptime: '10s',
    error_file: '/root/edu/logs/pm2-error.log',
    out_file: '/root/edu/logs/pm2-out.log',
    log_file: '/root/edu/logs/pm2-combined.log',
    time: true
  }]
};
