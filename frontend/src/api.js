// fetch 封装：统一 {code, data, message} 解包，code!=0 抛出并 toast
import { store } from './store.js'

/**
 * 通用请求。返回 data 字段；失败时 toast 并 throw Error(message)。
 */
async function request(method, path, body = null, isForm = false) {
  const options = { method, headers: {} }
  if (body !== null) {
    if (isForm) {
      // FormData 上传：不要手动设置 Content-Type（浏览器自动带 boundary）
      options.body = body
    } else {
      options.headers['Content-Type'] = 'application/json'
      options.body = JSON.stringify(body)
    }
  }
  let resp
  try {
    resp = await fetch(path, options)
  } catch (e) {
    toast('网络错误：后端未启动或不可达', 'error')
    throw new Error('网络错误：后端未启动或不可达')
  }
  let payload
  try {
    payload = await resp.json()
  } catch (e) {
    const msg = `响应解析失败（HTTP ${resp.status}）`
    toast(msg, 'error')
    throw new Error(msg)
  }
  if (payload.code !== 0) {
    toast(payload.message || '操作失败', 'error')
    throw new Error(payload.message || '操作失败')
  }
  return payload.data
}

function toast(message, type = 'info') {
  store.pushToast(message, type)
}

export const apiGet = (path) => request('GET', path)
export const apiPost = (path, body) => request('POST', path, body)
export const apiPut = (path, body) => request('PUT', path, body)
export const apiPatch = (path, body) => request('PATCH', path, body)
export const apiDelete = (path) => request('DELETE', path)
export const apiUpload = (path, formData) => request('POST', path, formData, true)

/** 把本地/相对图片路径转成可访问 URL（后端 /api/files/{path}） */
export function fileUrl(relPath) {
  if (!relPath) return ''
  if (relPath.startsWith('http')) return relPath
  const p = relPath.replace(/\\/g, '/').replace(/^data\//, '')
  return `/api/files/${p}`
}
