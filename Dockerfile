FROM node:16.17-buster as build
RUN npm install -g npm@8.19.2
USER node
EXPOSE 3000
WORKDIR /app


# docker build -t pcat .
# docker run --name pcat_app -p 127.0.0.1:3000:3000 -v ~/.bash_history:/home/node/.bash_history -v $(pwd):/app --rm -it pcat bash