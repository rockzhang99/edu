module.exports = {
  apps: [
    {
      name: "edu",
      script: "app.py",
      interpreter: "python",
      cwd: __dirname,
      env: {
        FLASK_HOST: "0.0.0.0",
        FLASK_PORT: "5000",
        FLASK_ENV: "production",
      },
    },
  ],
};
