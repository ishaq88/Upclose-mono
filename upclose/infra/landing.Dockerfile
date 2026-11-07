FROM node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
ARG VITE_APP_ORIGIN
ARG LANDING_ALLOW_LOCAL_APP=false
ENV VITE_APP_ORIGIN=$VITE_APP_ORIGIN
ENV LANDING_ALLOW_LOCAL_APP=$LANDING_ALLOW_LOCAL_APP
RUN npm run build:landing

FROM nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284
COPY infra/nginx.landing.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist-landing /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
