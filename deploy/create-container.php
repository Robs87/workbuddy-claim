<?php
// =============================================================================
// 通过 DockerMan 自己的 xmlToCommand() 建房。
// 保证容器带 net.unraid.docker.managed=dockerman 标签，与 WebUI 点击 Apply 完全一致。
//
// 用法（在 Unraid 主机上）：
//   php -d docroot=/usr/local/emhttp create-container.php <xml路径> [--recreate]
// =============================================================================
$docroot = '/usr/local/emhttp';
$_SERVER['DOCUMENT_ROOT'] = $docroot;

require_once "$docroot/webGui/include/Wrappers.php";
extract(parse_plugin_cfg('dynamix', true));

require_once "$docroot/plugins/dynamix.docker.manager/include/DockerClient.php";
require_once "$docroot/plugins/dynamix.docker.manager/include/Helpers.php";

$tmplPath = $argv[1] ?? '';
$recreate = in_array('--recreate', $argv, true);

if ($tmplPath === '' || !file_exists($tmplPath)) {
    fwrite(STDERR, "ERROR: template not found: $tmplPath\n");
    exit(1);
}

// xmlToCommand() 依赖这些外层作用域变量，缺失会导致 null 传参
$custom = DockerUtil::custom();
$subnet  = DockerUtil::network($custom);
$cpus    = DockerUtil::cpus();
$var     = parse_ini_file('/var/local/emhttp/var.ini') ?: [];
$driver  = DockerUtil::driver();

$DockerTemplates = new DockerTemplates();

$xml = simplexml_load_file($tmplPath);
$name = (string) $xml->Name;

$exists = trim(shell_exec('docker container inspect ' . escapeshellarg($name) . ' --format "{{.Id}}" 2>/dev/null') ?? '');
if ($exists !== '') {
    if (!$recreate) {
        fwrite(STDERR, "ERROR: container '$name' already exists. Re-run with --recreate to replace it.\n");
        exit(3);
    }
    fwrite(STDOUT, "Removing existing container $name ...\n");
    exec('docker rm -f ' . escapeshellarg($name) . ' 2>&1', $rmOut, $rmCode);
    if ($rmCode !== 0) {
        fwrite(STDERR, "ERROR: failed to remove $name: " . implode("\n", $rmOut) . "\n");
        exit($rmCode);
    }
}

[$cmd, $Name, $Repository] = xmlToCommand($tmplPath, true);
$runCmd = str_replace('/docker create ', '/docker run -d ', $cmd);

// 命令串里含令牌，绝不打印全文
fwrite(STDOUT, "Container name: $Name\n");
fwrite(STDOUT, "Repository: $Repository\n");
fwrite(STDOUT, "Command length: " . strlen($runCmd) . " bytes (含令牌，不显示)\n");

fwrite(STDOUT, "Creating + starting container...\n");
$out = [];
exec($runCmd . ' 2>&1', $out, $code);
fwrite(STDOUT, "docker output: " . implode("\n", $out) . "\n");
fwrite(STDOUT, "EXIT_CODE=$code\n");
exit($code);
