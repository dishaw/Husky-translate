$inputText = [Console]::In.ReadToEnd()

$inputText = $inputText -replace '(?m)^(\s*admin_passwd\s*=\s*).*$', '${1}CHANGE_ME'
$inputText = $inputText -replace '(?m)^(\s*db_password\s*=\s*).*$', '${1}CHANGE_ME'
$inputText = $inputText -replace '(?m)^(\s*-\s*PASSWORD=).*$', '${1}${ODOO_DB_PASSWORD:-CHANGE_ME}'
$inputText = $inputText -replace '(?m)^(\s*-\s*POSTGRES_PASSWORD=).*$', '${1}${POSTGRES_PASSWORD:-CHANGE_ME}'

[Console]::Out.Write($inputText)
