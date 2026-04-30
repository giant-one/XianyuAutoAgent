import sqlite3
import os
import json
from datetime import datetime
from loguru import logger


class ChatContextManager:
    """
    聊天上下文管理器
    
    负责存储和检索用户与商品之间的对话历史，使用SQLite数据库进行持久化存储。
    支持按会话ID检索对话历史，以及议价次数统计。
    """
    
    def __init__(self, max_history=100, db_path="data/chat_history.db"):
        """
        初始化聊天上下文管理器
        
        Args:
            max_history: 每个对话保留的最大消息数
            db_path: SQLite数据库文件路径
        """
        self.max_history = max_history
        self.db_path = db_path
        self._init_db()
        
    def _init_db(self):
        """初始化数据库表结构"""
        # 确保数据库目录存在
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
            
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建消息表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            chat_id TEXT
        )
        ''')
        
        # 检查是否需要添加chat_id字段（兼容旧数据库）
        cursor.execute("PRAGMA table_info(messages)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'chat_id' not in columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN chat_id TEXT')
            logger.info("已为messages表添加chat_id字段")
        
        # 创建索引以加速查询
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_item ON messages (user_id, item_id)
        ''')
        
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_chat_id ON messages (chat_id)
        ''')
        
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages (timestamp)
        ''')
        
        # 创建基于会话ID的议价次数表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_bargain_counts (
            chat_id TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建商品信息表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            price REAL,
            description TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 检查是否需要添加 is_multi_sku 字段（兼容旧数据库）
        cursor.execute("PRAGMA table_info(items)")
        item_columns = [col[1] for col in cursor.fetchall()]
        if 'is_multi_sku' not in item_columns:
            cursor.execute('ALTER TABLE items ADD COLUMN is_multi_sku INTEGER DEFAULT 0')
            logger.info("已为 items 表添加 is_multi_sku 字段")

        # 创建商品规格表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS item_skus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            sku_id TEXT,
            spec TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER DEFAULT 0,
            FOREIGN KEY (item_id) REFERENCES items(item_id)
        )
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_item_skus_item_id ON item_skus (item_id)
        ''')

        # 创建订单表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            buyer_id TEXT NOT NULL,
            paid_amount REAL,
            duration_days INTEGER,
            token TEXT,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            delivered_at DATETIME
        )
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_orders_item_id ON orders (item_id)
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_orders_buyer_id ON orders (buyer_id)
        ''')

        conn.commit()
        conn.close()
        logger.info(f"聊天历史数据库初始化完成: {self.db_path}")
        

            
    def save_item_info(self, item_id, item_data):
        """
        保存商品信息到数据库

        Args:
            item_id: 商品ID
            item_data: 商品信息字典
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # 从商品数据中提取有用信息
            price = float(item_data.get('soldPrice', 0))
            description = item_data.get('desc', '')

            # 解析 SKU 列表
            sku_rows = []
            raw_sku_list = item_data.get('skuList', [])
            for sku in raw_sku_list:
                specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
                spec_text = " ".join(specs) if specs else "默认规格"
                sku_price = round(sku.get('price', 0) / 100, 2)
                sku_stock = sku.get('quantity', 0)
                sku_id = str(sku.get('skuId', ''))
                sku_rows.append((item_id, sku_id, spec_text, sku_price, sku_stock))

            # 判断是否多规格：minPrice != maxPrice
            try:
                min_price = float(item_data.get('minPrice', price))
                max_price = float(item_data.get('maxPrice', price))
                is_multi_sku = 1 if min_price != max_price else 0
            except (TypeError, ValueError):
                is_multi_sku = 0

            # 将整个商品数据转换为JSON字符串
            data_json = json.dumps(item_data, ensure_ascii=False)
            now = datetime.now().isoformat()

            cursor.execute(
                """
                INSERT INTO items (item_id, data, price, description, is_multi_sku, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id)
                DO UPDATE SET data = ?, price = ?, description = ?, is_multi_sku = ?, last_updated = ?
                """,
                (
                    item_id, data_json, price, description, is_multi_sku, now,
                    data_json, price, description, is_multi_sku, now
                )
            )

            # 先清除旧 SKU，再批量插入
            cursor.execute("DELETE FROM item_skus WHERE item_id = ?", (item_id,))
            if sku_rows:
                cursor.executemany(
                    "INSERT INTO item_skus (item_id, sku_id, spec, price, stock) VALUES (?, ?, ?, ?, ?)",
                    sku_rows
                )

            conn.commit()
            logger.debug(f"商品信息已保存: {item_id}, 多规格: {bool(is_multi_sku)}, SKU 数量: {len(sku_rows)}")
        except Exception as e:
            logger.error(f"保存商品信息时出错: {e}")
            conn.rollback()
        finally:
            conn.close()
    
    def get_item_info(self, item_id):
        """
        从数据库获取商品信息
        
        Args:
            item_id: 商品ID
            
        Returns:
            dict: 商品信息字典，如果不存在返回None
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT data FROM items WHERE item_id = ?",
                (item_id,)
            )
            
            result = cursor.fetchone()
            if result:
                return json.loads(result[0])
            return None
        except Exception as e:
            logger.error(f"获取商品信息时出错: {e}")
            return None
        finally:
            conn.close()

    def get_item_skus(self, item_id):
        """
        获取商品的规格列表

        Args:
            item_id: 商品ID

        Returns:
            list: SKU 列表，每项包含 sku_id / spec / price / stock；不存在时返回空列表
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT sku_id, spec, price, stock FROM item_skus WHERE item_id = ? ORDER BY id ASC",
                (item_id,)
            )
            return [
                {"sku_id": row[0], "spec": row[1], "price": row[2], "stock": row[3]}
                for row in cursor.fetchall()
            ]
        except Exception as e:
            logger.error(f"获取商品规格时出错: {e}")
            return []
        finally:
            conn.close()

    def add_message_by_chat(self, chat_id, user_id, item_id, role, content):
        """
        基于会话ID添加新消息到对话历史
        
        Args:
            chat_id: 会话ID
            user_id: 用户ID (用户消息存真实user_id，助手消息存卖家ID)
            item_id: 商品ID
            role: 消息角色 (user/assistant)
            content: 消息内容
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 插入新消息，使用chat_id作为额外标识
            cursor.execute(
                "INSERT INTO messages (user_id, item_id, role, content, timestamp, chat_id) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, item_id, role, content, datetime.now().isoformat(), chat_id)
            )
            
            # 检查是否需要清理旧消息（基于chat_id）
            cursor.execute(
                """
                SELECT id FROM messages 
                WHERE chat_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?, 1
                """, 
                (chat_id, self.max_history)
            )
            
            oldest_to_keep = cursor.fetchone()
            if oldest_to_keep:
                cursor.execute(
                    "DELETE FROM messages WHERE chat_id = ? AND id < ?",
                    (chat_id, oldest_to_keep[0])
                )
            
            conn.commit()
        except Exception as e:
            logger.error(f"添加消息到数据库时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_context_by_chat(self, chat_id):
        """
        基于会话ID获取对话历史
        
        Args:
            chat_id: 会话ID
            
        Returns:
            list: 包含对话历史的列表
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                SELECT role, content FROM messages 
                WHERE chat_id = ? 
                ORDER BY timestamp ASC
                LIMIT ?
                """, 
                (chat_id, self.max_history)
            )
            
            messages = [{"role": role, "content": content} for role, content in cursor.fetchall()]
            
            # 获取议价次数并添加到上下文中
            bargain_count = self.get_bargain_count_by_chat(chat_id)
            if bargain_count > 0:
                messages.append({
                    "role": "system", 
                    "content": f"议价次数: {bargain_count}"
                })
            
        except Exception as e:
            logger.error(f"获取对话历史时出错: {e}")
            messages = []
        finally:
            conn.close()
        
        return messages

    def increment_bargain_count_by_chat(self, chat_id):
        """
        基于会话ID增加议价次数
        
        Args:
            chat_id: 会话ID
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 使用UPSERT语法直接基于chat_id增加议价次数
            cursor.execute(
                """
                INSERT INTO chat_bargain_counts (chat_id, count, last_updated)
                VALUES (?, 1, ?)
                ON CONFLICT(chat_id) 
                DO UPDATE SET count = count + 1, last_updated = ?
                """,
                (chat_id, datetime.now().isoformat(), datetime.now().isoformat())
            )
            
            conn.commit()
            logger.debug(f"会话 {chat_id} 议价次数已增加")
        except Exception as e:
            logger.error(f"增加议价次数时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_bargain_count_by_chat(self, chat_id):
        """
        基于会话ID获取议价次数
        
        Args:
            chat_id: 会话ID
            
        Returns:
            int: 议价次数
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT count FROM chat_bargain_counts WHERE chat_id = ?",
                (chat_id,)
            )
            
            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取议价次数时出错: {e}")
            return 0
        finally:
            conn.close()

    def save_order(self, order_id, item_id, buyer_id, paid_amount=None, duration_days=None):
        """
        保存订单信息

        Args:
            order_id: 订单ID
            item_id: 商品ID
            buyer_id: 买家ID
            paid_amount: 支付金额
            duration_days: 时长（天数）
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # 检查订单是否已存在
            cursor.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
            existing = cursor.fetchone()

            if existing:
                # 订单已存在，只更新需要更新的字段，保留 status、token、created_at、delivered_at
                cursor.execute(
                    """
                    UPDATE orders
                    SET item_id = ?, buyer_id = ?, paid_amount = ?, duration_days = ?
                    WHERE order_id = ?
                    """,
                    (item_id, buyer_id, paid_amount, duration_days, order_id)
                )
                logger.debug(f"订单已更新: {order_id}")
            else:
                # 订单不存在，插入新记录
                cursor.execute(
                    """
                    INSERT INTO orders (order_id, item_id, buyer_id, paid_amount, duration_days, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (order_id, item_id, buyer_id, paid_amount, duration_days)
                )
                logger.debug(f"订单已创建: {order_id}")

            conn.commit()
        except Exception as e:
            logger.error(f"保存订单时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_order(self, order_id):
        """
        获取订单信息

        Args:
            order_id: 订单ID

        Returns:
            dict: 订单信息，不存在返回None
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT order_id, item_id, buyer_id, paid_amount, duration_days, token, status, created_at, delivered_at FROM orders WHERE order_id = ?",
                (order_id,)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "order_id": row[0],
                    "item_id": row[1],
                    "buyer_id": row[2],
                    "paid_amount": row[3],
                    "duration_days": row[4],
                    "token": row[5],
                    "status": row[6],
                    "created_at": row[7],
                    "delivered_at": row[8]
                }
            return None
        except Exception as e:
            logger.error(f"获取订单时出错: {e}")
            return None
        finally:
            conn.close()

    def update_order_delivered(self, order_id, token):
        """
        更新订单为已发货状态

        Args:
            order_id: 订单ID
            token: 生成的token
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                UPDATE orders SET status = 'delivered', token = ?, delivered_at = ?
                WHERE order_id = ?
                """,
                (token, datetime.now().isoformat(), order_id)
            )
            conn.commit()
            logger.info(f"订单已更新为已发货: {order_id}")
        except Exception as e:
            logger.error(f"更新订单状态时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def is_order_delivered(self, order_id):
        """
        检查订单是否已发货

        Args:
            order_id: 订单ID

        Returns:
            bool: 是否已发货
        """
        order = self.get_order(order_id)
        if order:
            return order.get("status") == "delivered"
        return False

    def has_order_token(self, order_id):
        """
        检查订单是否已有token（用于判断是否已处理）

        Args:
            order_id: 订单ID

        Returns:
            bool: 是否有token
        """
        order = self.get_order(order_id)
        return order is not None and bool(order.get("token"))

    def get_order_by_user_and_item(self, user_id, item_id):
        """
        根据用户ID和商品ID查询订单（用于判断续费场景）

        Args:
            user_id: 用户ID
            item_id: 商品ID

        Returns:
            dict: 订单信息，不存在返回None
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT order_id, item_id, buyer_id, paid_amount, duration_days, token, status, created_at, delivered_at
                FROM orders
                WHERE buyer_id = ? AND item_id = ? AND token IS NOT NULL AND token != ''
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, item_id)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "order_id": row[0],
                    "item_id": row[1],
                    "buyer_id": row[2],
                    "paid_amount": row[3],
                    "duration_days": row[4],
                    "token": row[5],
                    "status": row[6],
                    "created_at": row[7],
                    "delivered_at": row[8]
                }
            return None
        except Exception as e:
            logger.error(f"查询用户商品订单时出错: {e}")
            return None
        finally:
            conn.close() 