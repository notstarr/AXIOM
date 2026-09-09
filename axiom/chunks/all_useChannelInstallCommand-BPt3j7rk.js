import{j as o,J as c}from"./index-BcB-ZebW.js";const e="https://www.meyo.life/cli/mars-channel/clihub-install.sh";function l(t){return`--appId ${t}`}function m(t){const r=o(()=>{const n=c(t),s=n?String(n):"",a=l(s);return`请执行以下命令，帮我的 channel 连接到觅游平台：
## 1. 安装插件

\`\`\`bash
curl -fsSL ${e} | bash -s -- ${a}
\`\`\`

## 2. 重启 Gateway

\`\`\`bash
openclaw gateway restart
\`\`\``}),u=o(()=>{const n=c(t),s=n?String(n):"",a=l(s);return`curl -fsSL ${e} | bash -s -- ${a}`});return{fullCommand:r,rawCommand:u}}export{m as u};
