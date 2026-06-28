FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY . /usr/share/nginx/html
RUN mkdir -p /usr/share/nginx/html/uploads \
    && chmod 777 /usr/share/nginx/html/uploads

EXPOSE 80
