# Dremes Agent — Railway Docker Deployment
FROM debian:12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages needed
RUN pip install git+https://github.com/nousresearch/hermes-agent.git google-genai pillow requests python-telegram-bot pinterest-dl playwright colorthief pyyaml python-dotenv hindsight-client --break-system-packages

# Install Chromium for Playwright (needed by drain_board.py)
RUN python3 -m playwright install chromium --with-deps

# Create hermes user
RUN useradd -m -u 10000 hermes

# Copy dremes repo
COPY --chown=hermes:hermes . /home/drewp/dremes-agent/

# Set working directory
WORKDIR /home/drewp/dremes-agent

# Create writable directories for the hermes user
RUN mkdir -p /data/workspace /data/.hermes /data/refs /home/drewp/dremes-agent/output/ad-approval \
    /home/drewp/dremes-agent/output/ads-bad /home/drewp/dremes-agent/output/posts \
    /home/drewp/dremes-agent/state /home/drewp/dremes-agent/website/public/images/ads \
    /home/drewp/dremes-agent/website/public/data && \
    chown -R hermes:hermes /data /home/drewp/dremes-agent/website/public \
    /home/drewp/dremes-agent/output /home/drewp/dremes-agent/state

# Hermes home for persistent state
ENV HERMES_HOME=/data/.hermes
ENV HOME=/data

# Expose gateway port and gallery port
EXPOSE 8642
EXPOSE 8080

# Copy and set up entrypoint
COPY start.sh /start.sh
COPY auto-sync.sh /home/drewp/dremes-agent/auto-sync.sh
RUN chmod +x /start.sh /home/drewp/dremes-agent/auto-sync.sh

CMD ["/start.sh"]
