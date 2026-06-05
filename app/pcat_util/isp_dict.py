isp_codes = {
  "46000": "China Mobile",
  "46001": "China Unicom",
  "46002": "China Unicom",
  "46003": "China Unicom",
  "46004": "China Unicom",
  "46005": "China Telecom",
  "46006": "China Telecom",
  "46007": "China Telecom",
  "46008": "China Telecom",
  "46009": "China Telecom",
  "46011": "China Telecom",
  "46015": "China Telecom",
  "46020": "China Telecom",
  "46692": "Chunghwa Telecom",
  "46601": "Far EasTone Telecom",
  "46697": "Taiwan Mobile Telecom",
  "46689": "T STAR"
}

isp_codes_cn = {
  "46000": "中国移动",
  "46001": "中国联通",
  "46002": "中国联通",
  "46003": "中国联通",
  "46004": "中国联通",
  "46005": "中国电信",
  "46006": "中国电信",
  "46007": "中国电信",
  "46008": "中国电信",
  "46009": "中国电信",
  "46011": "中国电信",
  "46015": "中国电信",
  "46020": "中国电信",
  "46692": "中华电信",
  "46601": "远传电信",
  "46697": "台湾大哥大",
  "46689": "T STAR"
}

def get_name(code, locale="EN"):
  if code is None:
    return ""
  
  if locale == "CN":
    name = isp_codes_cn.get(code)
  else:
    name = isp_codes.get(code)
    
  if name is None:
    return code
  else:
    return name