import base64
import json
import asyncio
import time
import os
import websockets
import requests
import re
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv, set_key
from XianyuApis import XianyuApis
import sys
import random


from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager
from reply_checker import RuleChecker


class XianyuLive:
    def __init__(self, cookies_str):
        self.xianyu = XianyuApis()
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.xianyu.session.cookies.update(self.cookies)  # 直接使用 session.cookies.update
        #self.myid = self.cookies['aui']
        self.myid = "1095520668"
        self.device_id = generate_device_id(self.myid)
        self.context_manager = ChatContextManager()
        
        # 心跳相关配置
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # 心跳间隔，默认15秒
        self.heartbeat_timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "5"))     # 心跳超时，默认5秒
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        
        # Token刷新相关配置
        self.token_refresh_interval = int(os.getenv("TOKEN_REFRESH_INTERVAL", "3600"))  # Token刷新间隔，默认1小时
        self.token_retry_interval = int(os.getenv("TOKEN_RETRY_INTERVAL", "300"))       # Token重试间隔，默认5分钟
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        
        # 人工接管相关配置
        self.manual_mode_conversations = set()  # 存储处于人工接管模式的会话ID
        self.manual_mode_timeout = int(os.getenv("MANUAL_MODE_TIMEOUT", "3600"))  # 人工接管超时时间，默认1小时
        self.manual_mode_timestamps = {}  # 记录进入人工模式的时间
        
        # 消息过期时间配置
        self.message_expire_time = int(os.getenv("MESSAGE_EXPIRE_TIME", "300000"))  # 消息过期时间，默认5分钟
        
        # 人工接管关键词，从环境变量读取
        self.toggle_keywords = os.getenv("TOGGLE_KEYWORDS", "。")
        
        # 模拟人工输入配置
        self.simulate_human_typing = os.getenv("SIMULATE_HUMAN_TYPING", "False").lower() == "true"

        # 预设规则拦截层
        self.rule_checker = RuleChecker()

    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info("开始刷新token...")
            
            # 获取新token（如果Cookie失效，get_token会直接退出程序）
            token_result = self.xianyu.get_token(self.device_id)
            if 'data' in token_result and 'accessToken' in token_result['data']:
                new_token = token_result['data']['accessToken']
                self.current_token = new_token
                self.last_token_refresh_time = time.time()
                logger.info("Token刷新成功")
                return new_token
            else:
                logger.error(f"Token刷新失败: {token_result}")
                return None
                
        except Exception as e:
            logger.error(f"Token刷新异常: {str(e)}")
            return None

    async def token_refresh_loop(self):
        """Token刷新循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要刷新token
                if current_time - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token即将过期，准备刷新...")
                    
                    new_token = await self.refresh_token()
                    if new_token:
                        logger.info("Token刷新成功，准备重新建立连接...")
                        # 设置连接重启标志
                        self.connection_restart_flag = True
                        # 关闭当前WebSocket连接，触发重连
                        if self.ws:
                            await self.ws.close()
                        break
                    else:
                        logger.error("Token刷新失败，将在{}分钟后重试".format(self.token_retry_interval // 60))
                        await asyncio.sleep(self.token_retry_interval)  # 使用配置的重试间隔
                        continue
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Token刷新循环出错: {e}")
                await asyncio.sleep(60)

    async def send_msg(self, ws, cid, toid, text):
        text = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        text_base64 = str(base64.b64encode(json.dumps(text).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": text_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def init(self, ws):
        # 如果没有token或者token过期，获取新token
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info("获取初始token...")
            await self.refresh_token()
        
        if not self.current_token:
            logger.error("无法获取有效token，初始化失败")
            raise Exception("Token获取失败")
            
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 等待一段时间，确保连接注册完成
        await asyncio.sleep(1)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')

    def is_chat_message(self, message):
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict) 
                and "1" in message 
                and isinstance(message["1"], dict)  # 确保是字典类型
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)  # 确保是字典类型
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and "@goofish" in message["1"][0]["1"]
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False
    
    def is_bracket_system_message(self, message):
        """检查是否为带中括号的系统消息"""
        try:
            if not message or not isinstance(message, str):
                return False
            
            clean_message = message.strip()
            # 检查是否以 [ 开头，以 ] 结尾
            if clean_message.startswith('[') and clean_message.endswith(']'):
                logger.debug(f"检测到系统消息: {clean_message}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查系统消息失败: {e}")
            return False

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return message_stripped in self.toggle_keywords

    def is_manual_mode(self, chat_id):
        """检查特定会话是否处于人工接管模式"""
        if chat_id not in self.manual_mode_conversations:
            return False
        
        # 检查是否超时
        current_time = time.time()
        if chat_id in self.manual_mode_timestamps:
            if current_time - self.manual_mode_timestamps[chat_id] > self.manual_mode_timeout:
                # 超时，自动退出人工模式
                self.exit_manual_mode(chat_id)
                return False
        
        return True

    def enter_manual_mode(self, chat_id):
        """进入人工接管模式"""
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()

    def exit_manual_mode(self, chat_id):
        """退出人工接管模式"""
        self.manual_mode_conversations.discard(chat_id)
        if chat_id in self.manual_mode_timestamps:
            del self.manual_mode_timestamps[chat_id]

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            return "manual"

    def _is_paid_message(self, message) -> bool:
        """检查消息是否为付款成功通知"""
        paid_keywords = [
            '我已付款，等待你发货',
            '已付款，待发货',
            '记得及时发货',
        ]
        try:
            reminder_content = message.get("1", {}).get("10", {}).get("reminderContent", "")
            for keyword in paid_keywords:
                if keyword in reminder_content:
                    return True
        except:
            pass
        return False
    
    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            return round(float(price) / 100, 2)
        except (ValueError, TypeError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0

    async def handle_auto_delivery(self, message, user_id):
        """处理虚拟商品自动发货"""
        try:
            # 从消息中提取订单信息
            reminder_url = message.get("1", {}).get("10", {}).get("reminderUrl", "")

            # 提取订单ID和商品ID
            order_id = None
            item_id = None

            # 优先从卡片数据中提取 orderId
            card_data = message.get("1", {}).get("6", {}).get("3", {}).get("4", "")
            if card_data:
                try:
                    card_json = json.loads(card_data)
                    # 从按钮的 targetUrl 中提取 orderId
                    button_url = card_json.get("dxCard", {}).get("button", {}).get("targetUrl", "")
                    if "orderId=" in button_url:
                        order_id = button_url.split("orderId=")[1].split("&")[0]
                        logger.debug(f"从卡片按钮URL中提取到订单ID: {order_id}")
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug(f"解析卡片数据失败: {e}")

            # 尝试从 reminderUrl 中提取
            if not order_id:
                if "orderId=" in reminder_url:
                    order_id = reminder_url.split("orderId=")[1].split("&")[0]
            if not item_id and "itemId=" in reminder_url:
                item_id = reminder_url.split("itemId=")[1].split("&")[0]

            if not order_id:
                logger.warning(f"无法从消息中提取订单ID")
                logger.debug(f"reminderUrl: {reminder_url}")
                logger.debug(f"cardData: {card_data[:200] if card_data else 'None'}...")
                return

            if not item_id:
                logger.warning(f"无法从消息中提取商品ID")
                return

            # 提取会话ID（session_id）
            session_id = message.get("1", {}).get("2", "").split("@")[0]

            # 检查是否已发货
            if self.context_manager.is_order_delivered(order_id):
                logger.info(f"订单 {order_id} 已发货，跳过")
                return

            logger.info(f"开始自动发货: 订单 {order_id}, 商品 {item_id}, 买家 {user_id}")

            # 保存订单到本地
            self.context_manager.save_order(order_id, item_id, user_id)

            # 获取订单支付信息
            payment_result = self.xianyu.get_order_payment_info(session_id, item_id)
            if not payment_result.get("success"):
                logger.error(f"获取订单支付信息失败: {order_id}")
                return

            # 提取支付金额
            payment_data = payment_result.get("data", {})
            middle_data = payment_data.get("middle", {}).get("data", {})
            paid_amount = float(middle_data.get("price", 0))

            logger.info(f"订单 {order_id} 支付金额: {paid_amount}")

            # 从商品信息获取规格和价格，计算购买时长
            skus = self.context_manager.get_item_skus(item_id)
            if not skus:
                # 从数据库获取商品信息
                item_info = self.context_manager.get_item_info(item_id)
                if not item_info:
                    # 从API获取商品信息
                    api_result = self.xianyu.get_item_info(item_id)
                    if 'data' in api_result and 'itemDO' in api_result['data']:
                        item_info = api_result['data']['itemDO']
                        self.context_manager.save_item_info(item_id, item_info)

                if item_info:
                    # 解析SKU
                    for sku in item_info.get('skuList', []):
                        specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
                        spec_text = " ".join(specs) if specs else "默认规格"
                        sku_price = round(sku.get('price', 0) / 100, 2)
                        sku_id = str(sku.get('skuId', ''))
                        skus.append({"sku_id": sku_id, "spec": spec_text, "price": sku_price})

            # 匹配支付金额对应的规格
            duration_days = self._calculate_duration(paid_amount, skus)

            if not duration_days:
                logger.error(f"无法根据支付金额 {paid_amount} 计算时长")
                return

            logger.info(f"订单 {order_id} 计算购买时长: {duration_days} 天")

            # 检查是否有现有token（续费场景），必须在 save_order 覆写之前读取
            existing_order = self.context_manager.get_order(order_id)
            token = existing_order.get("token") if existing_order else None

            # 更新订单信息
            self.context_manager.save_order(order_id, item_id, user_id, paid_amount, duration_days)

            if token:
                # 续费场景：延长token有效期
                logger.info(f"续费场景，为token {token} 延期 {duration_days} 天")
                token, subscribe_url, expire_date = await self._extend_token(token, duration_days)
            else:
                # 新购买场景：创建新token
                logger.info(f"新购买场景，创建 {duration_days} 天有效期token")
                token, subscribe_url, expire_date = await self._create_token(item_id, duration_days)

            if not token:
                logger.error(f"Token管理失败，订单 {order_id} 无法完成发货")
                return

            # 更新订单token
            self.context_manager.update_order_delivered(order_id, token)

            # TODO: 暂时注释掉自动发货，手动点击发货按钮
            # 调用闲鱼发货API
            # delivery_result = self.xianyu.auto_delivery(order_id, item_id)
            # if delivery_result.get("success"):
            #     logger.info(f"✅ 自动发货成功: 订单 {order_id}")
            # else:
            #     logger.error(f"❌ 自动发货失败: 订单 {order_id}, 错误: {delivery_result.get('error')}")

            # 发送token给买家
            await self.send_token_message(session_id, user_id, token, duration_days, subscribe_url, expire_date)

        except Exception as e:
            logger.error(f"处理自动发货时发生错误: {str(e)}")

    def _calculate_duration(self, paid_amount, skus):
        """根据支付金额和SKU计算购买时长（天数）"""
        try:
            # 按价格升序排列SKU
            sorted_skus = sorted(skus, key=lambda x: x.get("price", 0))

            # 精确匹配优先
            for sku in sorted_skus:
                if abs(sku.get("price", 0) - paid_amount) < 0.01:
                    return self._parse_duration_from_spec(sku.get("spec", ""))

            # 向下匹配（取最接近且不超过的价格对应的时长）
            for sku in sorted_skus:
                if sku.get("price", 0) <= paid_amount:
                    return self._parse_duration_from_spec(sku.get("spec", ""))

            return None
        except Exception as e:
            logger.error(f"计算时长失败: {e}")
            return None

    def _parse_duration_from_spec(self, spec):
        """从规格文本解析时长"""
        spec_lower = spec.lower()

        # 匹配各种时长格式
        patterns = [
            (r"1\s*年|一年|12\s*个月|12\s*月", 365),
            (r"2\s*年|两年|24\s*个月|24\s*月", 730),
            (r"6\s*个月|半年|6\s*月", 180),
            (r"(\d+)\s*天", lambda m: int(m.group(1))),
            (r"(\d+)\s*个月", lambda m: int(m.group(1)) * 30),
            (r"月卡", 30),
            (r"季卡|季度", 90),
        ]

        for pattern, result in patterns:
            match = re.search(pattern, spec_lower)
            if match:
                if callable(result):
                    return result(match)
                return result

        return None

    async def _create_token(self, item_id, expire_days):
        """调用token管理系统创建token"""
        try:
            api_url = os.getenv("TOKEN_API_URL", "http://localhost:8100/api.php")
            api_key = os.getenv("TOKEN_API_KEY", "your_api_key_here")

            payload = {
                "action": "create_token",
                "playlist_ids": [2],
                "expire_days": expire_days,
                "channel": "xianyu_auto",
                "max_ip_per_day": 4
            }

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(api_url, json=payload, headers=headers, timeout=30)
            result = response.json()

            if result.get("success") or result.get("code") == 0:
                token = result.get("data", {}).get("token") or result.get("token")
                subscribe_url = result.get("data", {}).get("subscribe_url", "")
                expire_date = result.get("data", {}).get("expire_date", "")
                logger.info(f"Token延期成功: {token} +{expire_days}天，过期时间：{expire_date}")
                logger.info(f"订阅链接: {subscribe_url}")
                return (token, subscribe_url, expire_date)
            else:
                logger.error(f"Token创建失败: {result}")
                return (None, None, None)

        except Exception as e:
            logger.error(f"创建Token异常: {str(e)}")
            return (None, None, None)

    async def _extend_token(self, token, add_days):
        """调用token管理系统延期token，返回 (token, subscribe_url)"""
        try:
            api_url = os.getenv("TOKEN_API_URL", "http://localhost:8100/api.php")
            api_key = os.getenv("TOKEN_API_KEY", "your_api_key_here")

            payload = {
                "action": "update_token",
                "token": token,
                "add_days": add_days,
                "status": 1
            }

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            response = requests.post(api_url, json=payload, headers=headers, timeout=30)
            result = response.json()

            if result.get("success") or result.get("code") == 0:
                # 延期成功后，生成订阅链接
                subscribe_url = result.get("data", {}).get("subscribe_url", "")
                expire_date = result.get("data", {}).get("expire_date", "")
                logger.info(f"Token延期成功: {token} +{add_days}天，过期时间：{expire_date}")
                logger.info(f"订阅链接: {subscribe_url}")
                return (token, subscribe_url, expire_date)
            else:
                logger.error(f"Token延期失败: {result}")
                return (None, None, None)

        except Exception as e:
            logger.error(f"延期Token异常: {str(e)}")
            return (None, None, None)

    async def send_token_message(self, chat_id, user_id, token, duration_days, subscribe_url, expire_date):
        """发送token给买家（拆分成4条消息）"""
        try:
            # 消息1：有效期
            msg1 = f"📅 有效期：{duration_days} 天（至 {expire_date} 到期）"

            # 消息2：订阅链接
            msg2 = f"🔗 订阅地址：\n{subscribe_url}"

            # 消息3：售后服务说明
            msg3 = """👆🏻将上方源地址添加到订阅就行，不要用🪜。

关于售后：
1、免费试用：
下单后的 12小时内 为免费试用期。如不满，请联系我为您关闭订单，不会产生任何费用。
⭕️⭕️不要自己点退款，不满意联系我来关闭订单
⭕️⭕️不要自己点退款，不满意联系我来关闭订单
2、支持随时退款：
📦🔙月卡退款规则
每3天为一个扣费周期。每月扣除金额为卡价格的1/10。
退款金额 = 月卡价格 - (月卡价格 ÷ 10 × 已使用天数 ÷ 3)
📦🔙半年/年卡退款规则
年卡按对应的月度费用计算已使用部分，不足一个月按一个月计算。
退款金额 = 年卡价格 - (月卡销售价格 × 已使用整月数)

❗本产品为个人订阅❗严禁多设备❗严禁频繁切换网络❗严禁传播，否则将被限制

❗❗对同行、中差评、骚扰等行为，将直接拉黑封卡。

[沙发]兼容大部分APP，
安卓全设备推荐：OK影视 app
苹果全设备推荐：APTV app
Windows安装PotPlayer（体验没有电视好）或者安卓模拟器使用。

配置教程：http://154.64.255.221:5080/doc/jc/index.html
（使用前请先查看，节省时间和操作难度）"""

            # 消息4：常见问题处理
            msg4 = """[挥手]部分app会遇到无法播放的情况，需到设置中将User-Agent改为

okhttp/3.12.1"""

            # 依次发送消息，每条间隔0.5秒
            await self.send_msg(self.ws, chat_id, user_id, msg1)
            await asyncio.sleep(0.5)
            await self.send_msg(self.ws, chat_id, user_id, msg2)
            await asyncio.sleep(0.5)
            await self.send_msg(self.ws, chat_id, user_id, msg3)
            await asyncio.sleep(0.5)
            await self.send_msg(self.ws, chat_id, user_id, msg4)

            logger.info(f"Token消息已发送: Token={token[:16]}...")
            logger.info(f"会话ID：{chat_id},订阅链接: {subscribe_url}")

        except Exception as e:
            logger.error(f"发送Token消息失败: {str(e)}")
    
    def build_item_description(self, item_info):
        """构建商品描述"""
        
        # 处理 SKU 列表
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        
        for sku in raw_sku_list:
            # 提取规格文本
            specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
            spec_text = " ".join(specs) if specs else "默认规格"
            
            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": sku.get('quantity', 0)
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]
        
        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            # 如果没有SKU价格，回退使用商品主价格
            main_price = round(float(item_info.get('soldPrice', 0)), 2)
            price_display = f"¥{main_price}"

        summary = {
            "title": item_info.get('title', ''),
            "desc": item_info.get('desc', ''),
            "price_range": price_display,
            "total_stock": item_info.get('quantity', 0),
            "sku_details": clean_skus
        }

        return json.dumps(summary, ensure_ascii=False)

    async def handle_message(self, message_data, websocket):
        """处理所有类型的消息"""
        try:

            try:
                message = message_data
                ack = {
                    "code": 200,
                    "headers": {
                        "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                        "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                    }
                }
                if 'app-key' in message["headers"]:
                    ack["headers"]["app-key"] = message["headers"]["app-key"]
                if 'ua' in message["headers"]:
                    ack["headers"]["ua"] = message["headers"]["ua"]
                if 'dt' in message["headers"]:
                    ack["headers"]["dt"] = message["headers"]["dt"]
                await websocket.send(json.dumps(ack))
            except Exception as e:
                pass

            # 如果不是同步包消息，直接返回
            if not self.is_sync_package(message_data):
                return

            # 获取并解密数据
            sync_data = message_data["body"]["syncPushPackage"]["data"][0]
            
            # 检查是否有必要的字段
            if "data" not in sync_data:
                logger.debug("同步包中无data字段")
                return

            # 解密数据
            try:
                data = sync_data["data"]
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    data = json.loads(data)
                    # logger.info(f"无需解密 message: {data}")
                    return
                except Exception as e:
                    # logger.info(f'加密数据: {data}')
                    decrypted_data = decrypt(data)
                    message = json.loads(decrypted_data)
            except Exception as e:
                logger.error(f"消息解密失败: {e}")
                return

            try:
                # 判断是否为订单消息
                user_id = message['1'].split('@')[0]
                user_url = f'https://www.goofish.com/personal?userId={user_id}'
                red_reminder = message['3'].get('redReminder', '')

                if red_reminder == '等待买家付款':
                    logger.info(f'等待买家 {user_url} 付款')
                    return
                elif red_reminder == '交易关闭':
                    logger.info(f'买家 {user_url} 交易关闭')
                    return

                # 付款消息单独数据结构
                red_reminder = message['10'].get('redReminder', '')
                if red_reminder == '等待卖家发货' or self._is_paid_message(message):
                    logger.info(f'交易成功 {user_url} 等待卖家发货')

                    # 自动发货处理
                    await self.handle_auto_delivery(message, user_id)
                    return
            except:
                pass

            # 判断消息类型
            if self.is_typing_status(message):
                logger.debug("用户正在输入")
                return
            elif not self.is_chat_message(message):
                logger.debug("其他非聊天消息")
                logger.debug(f"原始消息: {message}")
                return

            # 处理聊天消息
            create_time = int(message["1"]["5"])
            send_user_name = message["1"]["10"]["reminderTitle"]
            send_user_id = message["1"]["10"]["senderUserId"]
            send_message = message["1"]["10"]["reminderContent"]
            
            # 时效性验证（过滤5分钟前消息）
            if (time.time() * 1000 - create_time) > self.message_expire_time:
                logger.debug("过期消息丢弃")
                return
                
            # 获取商品ID和会话ID
            url_info = message["1"]["10"]["reminderUrl"]
            item_id = url_info.split("itemId=")[1].split("&")[0] if "itemId=" in url_info else None
            chat_id = message["1"]["2"].split('@')[0]
            
            if not item_id:
                logger.warning("无法获取商品ID")
                return

            # 检查是否为卖家（自己）发送的控制命令
            if send_user_id == self.myid:
                logger.debug("检测到卖家消息，检查是否为控制命令")
                
                # 检查切换命令
                if self.check_toggle_keywords(send_message):
                    mode = self.toggle_manual_mode(chat_id)
                    if mode == "manual":
                        logger.info(f"🔴 已接管会话 {chat_id} (商品: {item_id})")
                    else:
                        logger.info(f"🟢 已恢复会话 {chat_id} 的自动回复 (商品: {item_id})")
                    return

                # 热重载预设规则
                if send_message.strip() == "#reload_rules":
                    self.rule_checker.reload()
                    logger.info("[规则热重载] 卖家触发规则重载成功")
                    return
                
                # 记录卖家人工回复
                self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", send_message)
                logger.info(f"卖家人工回复 (会话: {chat_id}, 商品: {item_id}): {send_message}")
                return
            
            logger.info(f"用户: {send_user_name} (ID: {send_user_id}), 商品: {item_id}, 会话: {chat_id}, 消息: {send_message}")
            
            
            # 如果当前会话处于人工接管模式，不进行自动回复
            if self.is_manual_mode(chat_id):
                logger.info(f"🔴 会话 {chat_id} 处于人工接管模式，跳过自动回复")
                # 添加用户消息到上下文
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                return
            # 检查是否为带中括号的系统消息
            if self.is_bracket_system_message(send_message):
                logger.info(f"检测到系统消息：'{send_message}'，跳过自动回复")
                return
            if self.is_system_message(message):
                logger.debug("系统消息，跳过处理")
                return
            # 从数据库中获取商品信息，如果不存在则从API获取并保存
            item_info = self.context_manager.get_item_info(item_id)
            if not item_info:
                logger.info(f"从API获取商品信息: {item_id}")
                api_result = self.xianyu.get_item_info(item_id)
                if 'data' in api_result and 'itemDO' in api_result['data']:
                    item_info = api_result['data']['itemDO']
                    # 保存商品信息到数据库
                    self.context_manager.save_item_info(item_id, item_info)
                else:
                    logger.warning(f"获取商品信息失败: {api_result}")
                    return
            else:
                logger.info(f"从数据库获取商品信息: {item_id}")
                
            item_description=f"当前商品的信息如下：{self.build_item_description(item_info)}"
            
            # 获取完整的对话上下文
            context = self.context_manager.get_context_by_chat(chat_id)

            # 预设规则拦截层（命中则跳过 LLM 调用，减少LLM调用次数）
            rule_replies = self.rule_checker.match(send_message)
            if rule_replies:
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)

                # 计算总消息长度（用于延迟计算）
                total_length = sum(len(msg) for msg in rule_replies)

                # 添加所有回复到上下文
                for reply in rule_replies:
                    self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", reply)

                if self.simulate_human_typing:
                    base_delay = random.uniform(0, 1)
                    typing_delay = total_length * random.uniform(0.1, 0.3)
                    await asyncio.sleep(min(base_delay + typing_delay, 10.0))

                # 发送每条消息（多条消息间有短暂延迟）
                for i, reply in enumerate(rule_replies):
                    await self.send_msg(websocket, chat_id, send_user_id, reply)
                    if i < len(rule_replies) - 1:
                        await asyncio.sleep(0.5)  # 多条消息间间隔 0.5 秒

                return

            # 生成回复
            bot_reply = bot.generate_reply(
                send_message,
                item_description,
                context=context
            )
            
            # 检查是否需要回复
            if bot_reply == "-":
                logger.info(f"[无需回复] 用户 {send_user_name} 的消息被识别为无需回复类型")
                return
            
            # 添加用户消息到上下文
            self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
            
            # 检查是否为价格意图，如果是则增加议价次数
            if bot.last_intent == "price":
                self.context_manager.increment_bargain_count_by_chat(chat_id)
                bargain_count = self.context_manager.get_bargain_count_by_chat(chat_id)
                logger.info(f"用户 {send_user_name} 对商品 {item_id} 的议价次数: {bargain_count}")
            
            # 添加机器人回复到上下文
            self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", bot_reply)
            
            logger.info(f"机器人回复: {bot_reply}")
            
            # 模拟人工输入延迟
            if self.simulate_human_typing:
                # 基础延迟 0-1秒 + 每字 0.1-0.3秒
                base_delay = random.uniform(0, 1)
                typing_delay = len(bot_reply) * random.uniform(0.1, 0.3)
                total_delay = base_delay + typing_delay
                # 设置最大延迟上限，防止过长回复等待太久
                total_delay = min(total_delay, 10.0)
                
                logger.info(f"模拟人工输入，延迟发送 {total_delay:.2f} 秒...")
                await asyncio.sleep(total_delay)
                
            await self.send_msg(websocket, chat_id, send_user_id, bot_reply)
            
        except Exception as e:
            logger.error(f"处理消息时发生错误: {str(e)}")
            logger.debug(f"原始消息: {message_data}")

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            logger.error(f"发送心跳包失败: {e}")
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)
                
                # 检查上次心跳响应时间，如果超时则认为连接已断开
                if (current_time - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，可能连接已断开")
                    break
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"心跳循环出错: {e}")
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if (
                isinstance(message_data, dict)
                and "headers" in message_data
                and "mid" in message_data["headers"]
                and "code" in message_data
                and message_data["code"] == 200
            ):
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {e}")
        return False

    async def main(self):
        while True:
            try:
                # 重置连接重启标志
                self.connection_restart_flag = False
                
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    
                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    
                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    
                    # 启动token刷新任务
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                    
                    async for message in websocket:
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break
                                
                            message_data = json.loads(message)
                            
                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue
                            
                            # 发送通用ACK响应
                            if "headers" in message_data and "mid" in message_data["headers"]:
                                ack = {
                                    "code": 200,
                                    "headers": {
                                        "mid": message_data["headers"]["mid"],
                                        "sid": message_data["headers"].get("sid", "")
                                    }
                                }
                                # 复制其他可能的header字段
                                for key in ["app-key", "ua", "dt"]:
                                    if key in message_data["headers"]:
                                        ack["headers"][key] = message_data["headers"][key]
                                await websocket.send(json.dumps(ack))
                            
                            # 处理其他消息
                            await self.handle_message(message_data, websocket)
                                
                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error(f"处理消息时发生错误: {str(e)}")
                            logger.debug(f"原始消息: {message}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                
            except Exception as e:
                logger.error(f"连接发生错误: {e}")
                
            finally:
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                        
                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass
                
                # 如果是主动重启，立即重连；否则等待5秒
                if self.connection_restart_flag:
                    logger.info("主动重启连接，立即重连...")
                else:
                    logger.info("等待5秒后重连...")
                    await asyncio.sleep(5)



def check_and_complete_env():
    """检查并补全关键环境变量"""
    # 定义关键变量及其默认无效值（占位符）
    critical_vars = {
        "API_KEY": "默认使用通义千问,apikey通过百炼模型平台获取",
        "COOKIES_STR": "your_cookies_here"
    }
    
    env_path = ".env"
    updated = False
    
    for key, placeholder in critical_vars.items():
        curr_val = os.getenv(key)
        
        # 如果变量未设置，或者值等于占位符
        if not curr_val or curr_val == placeholder:
            logger.warning(f"配置项 [{key}] 未设置或为默认值，请输入")
            while True:
                val = input(f"请输入 {key}: ").strip()
                if val:
                    # 更新当前环境
                    os.environ[key] = val
                    
                    # 尝试持久化到 .env
                    try:
                        # 如果没有.env文件，先创建
                        if not os.path.exists(env_path):
                            with open(env_path, 'w', encoding='utf-8') as f:
                                pass # Create empty file
                        
                        set_key(env_path, key, val)
                        updated = True
                    except Exception as e:
                        logger.warning(f"无法自动写入.env文件，请手动保存: {e}")
                    break
                else:
                    print(f"{key} 不能为空，请重新输入")
    
    if updated:
        logger.info("新的配置已保存/更新至 .env 文件中")


if __name__ == '__main__':
    # 加载环境变量
    if os.path.exists(".env"):
        load_dotenv()
        logger.info("已加载 .env 配置")
    
    if os.path.exists(".env.example"):
        load_dotenv(".env.example")  # 不会覆盖已存在的变量
        logger.info("已加载 .env.example 默认配置")
    
    # 配置日志级别
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.remove()  # 移除默认handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    logger.info(f"日志级别设置为: {log_level}")
    
    # 交互式检查并补全配置
    check_and_complete_env()
    
    cookies_str = os.getenv("COOKIES_STR")
    bot = XianyuReplyBot()
    xianyuLive = XianyuLive(cookies_str)
    # 常驻进程
    asyncio.run(xianyuLive.main())
