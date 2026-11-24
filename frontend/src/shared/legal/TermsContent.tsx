/**
 * 服务条款与隐私说明共享正文（供首页弹窗与 /tos 独立页复用，位于 shared 层无上层依赖）。
 */
export interface TermsContentProps {
  retentionHours?: number;
  submissionAuditEnabled?: boolean;
}

export function TermsContent({ retentionHours = 24, submissionAuditEnabled }: TermsContentProps) {
  const retentionText =
    retentionHours > 0
      ? `原文、译文与导出缓存默认在服务器保留 ${retentionHours} 小时，每 10 分钟检查清理一次（正在运行的任务将延后清理），完成后请及时下载。`
      : "当前站点未设置自动过期清理周期，但仍建议任务完成后及时下载到本地保存。";

  return (
    <div className="tos-sections">
      <section>
        <h3>1. 文档解析与翻译数据传输</h3>
        <ul>
          <li>
            上传的 PDF 将发送至所选云端版面解析服务（默认 MinerU，站点开放时可选
            PaddleOCR-VL）进行版面、公式与图表识别。
          </li>
          <li>
            <strong>自备 API Key 模式</strong>：正文片段将转发至你在“设置”中指定的翻译服务商接口；
            <strong>免费体验模式</strong>：正文片段将发往本站配置的公共免费推理接口。
          </li>
          <li>
            <strong>请勿上传涉密、未公开或包含敏感个人隐私的文档</strong>；高峰时段云端版面解析可能排队 10 分钟以上。
          </li>
        </ul>
      </section>

      <section>
        <h3>2. 文件保留期限与自动删除</h3>
        <ul>
          <li>{retentionText}</li>
          <li>在线对照阅读、逐段编辑和 PDF 下载均依赖保留期内的临时资料，过期清理后无法恢复。</li>
        </ul>
      </section>

      <section>
        <h3>3. 个人密钥与本机浏览器数据</h3>
        <ul>
          <li>
            你在前台“设置”中填写的翻译 API Key、个人 MinerU
            Token、自定义术语库、历史任务入口、文件指纹与阅读进度均仅保存在当前浏览器本地，本站后端不落盘保存访客密钥。
          </li>
          <li>清除浏览器站点数据或更换设备后，上述本地记录将随之清空。</li>
        </ul>
      </section>

      <section>
        <h3>4. 统计与提交审计说明</h3>
        <ul>
          <li>公开“统计”页仅展示最近 7 天的全站聚合数量（访问浏览器数按随机标识跨日去重，不关联个人身份）。</li>
          <li>
            {submissionAuditEnabled === true ? (
              <>
                <strong>当前站点已开启提交审计</strong>
                ：系统会留存论文标题（识别失败时回退为上传文件名）、提交时间、页数、通道方式、可信提交 IP 与客户端
                User-Agent（免费体验与自备 Key
                模式均适用），仅限管理员用于故障排查与安全审计，并随运维元数据备份长期保留（备份采用白名单机制，严格排除正文与一切密钥/代理凭据）。
              </>
            ) : submissionAuditEnabled === false ? (
              <>
                <strong>当前站点未开启提交审计</strong>：系统仅做匿名终端维度聚合统计，不留存单次提交的论文标题、IP
                或原始 User-Agent。
              </>
            ) : (
              <>
                若站点管理员显式开启了提交审计，系统会留存论文标题（识别失败时回退为文件名）、提交时间、通道方式、可信提交
                IP 与客户端 User-Agent（仅管理员排查可见，严格排除正文与一切密钥）；未开启时仅做匿名聚合统计。
              </>
            )}
          </li>
        </ul>
      </section>

      <section>
        <h3>5. 开源协议与免责声明</h3>
        <ul>
          <li>
            本项目为开源自部署学术阅读辅助工具，按“现状”提供；机器翻译与版面还原可能存在术语偏差或排版误差，正式引用与发表请以原文为准并自行核对。
          </li>
          <li>使用本服务即表示你已阅读并同意上述数据流转、保留期限及合规边界条款。</li>
        </ul>
      </section>
    </div>
  );
}
