FROM node:24-slim AS build
WORKDIR /app
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY apps/dashboard/ ./
RUN npm run build

FROM node:24-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app /app
EXPOSE 3001
CMD ["npm", "run", "start"]
