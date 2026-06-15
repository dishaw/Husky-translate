ARG ODOO_BASE_IMAGE=odoo-translator:v1
FROM ${ODOO_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=

USER root

RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s/deb.debian.org/$APT_MIRROR/g" /etc/apt/sources.list.d/debian.sources || true; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        fonts-wqy-zenhei \
        tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

RUN rm -rf /usr/lib/python3*/dist-packages/typing_extensions* 2>/dev/null || true

RUN pip3 install --break-system-packages -i "$PIP_INDEX_URL" \
        "typing_extensions>=4.12.0" \
        python-docx \
        python-pptx \
        PyMuPDF \
        requests \
        markdownify \
        numpy \
        emoji \
        markdown2 \
        "pydantic>=2.0.0" \
        mcp \
        jinja2 \
        pyyaml \
        jsonschema

USER odoo
