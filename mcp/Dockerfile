FROM python:3.11-slim
WORKDIR /srv
COPY mcp/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY mcp/ /srv/service/
COPY app/ /srv/portal_app/
ENV PYTHONPATH=/srv/service:/srv/portal_app
EXPOSE 8101 8102
CMD ["python", "-m", "service.mcp_server"]
