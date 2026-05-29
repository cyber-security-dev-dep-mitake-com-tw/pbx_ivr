FROM andrius/asterisk:22

# Install Python 3 for AGI scripts
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY etc/asterisk/ /etc/asterisk/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 5060/udp
EXPOSE 8088
EXPOSE 10000-10100/udp

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["asterisk", "-f", "-vvv"]
