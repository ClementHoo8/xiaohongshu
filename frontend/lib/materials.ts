import type { Attachment, Material, MaterialScope } from "@/lib/api"

export const MATERIAL_SCOPE_LABELS: Record<MaterialScope, string> = {
  product: "品类相关",
  general: "通用灵感",
}

export const PRODUCT_CONTENT_TYPES = [
  "养宠痛点",
  "专业知识分享",
  "适口性反馈",
  "品类知识",
  "产品卖点",
  "用户案例",
  "喂养场景",
  "竞品种草",
]

export const GENERAL_CONTENT_TYPES = [
  "笔记灵感",
  "爆款参考",
  "标题灵感",
  "视频灵感",
  "活动素材",
]

// 全量选项由两组拼接得出，避免维护多份互相漂移的列表。
export const CONTENT_TYPES = [...PRODUCT_CONTENT_TYPES, ...GENERAL_CONTENT_TYPES]

export const TITLE_INSPIRATION_TYPES = [
  "痛点型",
  "钩子型",
  "反差型",
  "提问型",
  "数字型",
  "干货型",
  "情绪型",
  "场景型",
] as const

export const SOURCE_TYPE_LABELS: Record<string, string> = {
  self_experience: "自家经验",
  product资料: "产品资料",
  customer_feedback: "客户反馈",
  xiaohongshu: "小红书博主",
  douyin: "抖音博主",
  bilibili: "B站内容",
  competitor: "竞品账号",
  pet_group: "养宠社群",
  vet: "宠物医生",
  breeder: "繁育人",
  sales_feedback: "销售反馈",
  wechat_article: "公众号文章",
  other: "其他",
}

export function filterMaterials(materials: Material[], query: string) {
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN")
  if (!normalizedQuery) return materials

  return materials.filter((material) =>
    [
      material.title,
      material.summary,
      material.original_content,
      material.brand,
      material.category,
      material.author,
      MATERIAL_SCOPE_LABELS[material.material_scope],
      SOURCE_TYPE_LABELS[material.source_type],
      ...material.content_types,
      ...material.tags,
    ].some((value) => value?.toLocaleLowerCase("zh-CN").includes(normalizedQuery))
  )
}

export function formatMaterialDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
  }).format(new Date(value))
}

export function getPreviewImage(material: Material) {
  return material.attachments?.find(isImageAttachment)
}

export function isImageAttachment(attachment: Attachment) {
  if (attachment.type?.startsWith("image/")) return true

  const pathWithoutQuery = attachment.path.split("?", 1)[0]
  return /\.(avif|bmp|gif|jpe?g|png|svg|webp)$/i.test(pathWithoutQuery)
}

export function isVideoAttachment(attachment: Attachment) {
  if (attachment.type?.startsWith("video/")) return true

  const pathWithoutQuery = attachment.path.split("?", 1)[0]
  return /\.(m4v|mov|mp4|webm)$/i.test(pathWithoutQuery)
}
