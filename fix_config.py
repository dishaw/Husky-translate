import re, os

with open('/var/www/onlyoffice/WebStudio/web.appsettings.config', 'r') as f:
    c = f.read()

# Fix public URL: use nginx proxy path instead of internal Docker hostname
c = c.replace(
    '<add key="files.docservice.url.public" value="http://document-server/" />',
    '<add key="files.docservice.url.public" value="/ds-vpath/" />'
)
c = c.replace(
    '<add key="files.docservice.url.public" value="http://document-server:80/" />',
    '<add key="files.docservice.url.public" value="/ds-vpath/" />'
)

with open('/var/www/onlyoffice/WebStudio/web.appsettings.config', 'w') as f:
    f.write(c)

# Also fix TeamLabSvc config
svc_config = '/var/www/onlyoffice/Services/TeamLabSvc/TeamLabSvc.exe.config'
if os.path.exists(svc_config):
    with open(svc_config, 'r') as f:
        c = f.read()
    c = c.replace(
        '<add key="files.docservice.url.public" value="http://document-server/" />',
        '<add key="files.docservice.url.public" value="/ds-vpath/" />'
    )
    c = c.replace(
        '<add key="files.docservice.url.public" value="http://document-server:80/" />',
        '<add key="files.docservice.url.public" value="/ds-vpath/" />'
    )
    with open(svc_config, 'w') as f:
        f.write(c)

print("Public URL fixed to /ds-vpath/")
