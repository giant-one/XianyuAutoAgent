#!/usr/bin/env python3
"""
API 测试脚本 - 用于测试闲鱼接口是否可用
"""

import os
import sys
import json
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

from XianyuApis import XianyuApis
from utils.xianyu_utils import trans_cookies


def test_get_order_payment_info():
    """测试获取订单支付信息接口"""
    print("\n" + "=" * 50)
    print("测试 1: get_order_payment_info")
    print("=" * 50)

    xianyu = XianyuApis()

    # 设置 cookie
    cookies_str = os.getenv("COOKIES_STR")
    cookies = trans_cookies(cookies_str)
    xianyu.session.cookies.update(cookies)

    # 测试参数
    # session_id = "60559684926"
    # item_id = "1002799610955"
    session_id = "60476421644"
    item_id = "1003480140006"

    print(f"session_id: {session_id}")
    print(f"item_id: {item_id}")

    result = xianyu.get_order_payment_info(session_id, item_id)

    print("\n返回结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("success"):
        data = result.get("data", {})
        middle = data.get("middle", {}).get("data", {})
        price = middle.get("price")
        print(f"\n✅ 成功！支付金额: ¥{price}")
        return True
    else:
        print(f"\n❌ 失败: {result.get('error')}")
        return False


def test_auto_delivery():
    """测试自动发货接口"""
    print("\n" + "=" * 50)
    print("测试 2: auto_delivery (谨慎运行!)")
    print("=" * 50)

    # 确认提示
    confirm = input("自动发货接口会实际发货，是否继续测试? (yes/no): ")
    if confirm.lower() != "yes":
        print("跳过自动发货测试")
        return None

    xianyu = XianyuApis()

    # 设置 cookie
    cookies_str = os.getenv("COOKIES_STR")
    cookies = trans_cookies(cookies_str)
    xianyu.session.cookies.update(cookies)

    order_id = input("请输入订单ID: ").strip()
    item_id = input("请输入商品ID: ").strip()

    if not order_id or not item_id:
        print("订单ID和商品ID不能为空")
        return None

    print(f"\norder_id: {order_id}")
    print(f"item_id: {item_id}")

    result = xianyu.auto_delivery(order_id, item_id)

    print("\n返回结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("success"):
        print("\n✅ 发货成功!")
        return True
    else:
        print(f"\n❌ 发货失败: {result.get('error')}")
        return False


def test_get_item_info():
    """测试获取商品信息接口"""
    print("\n" + "=" * 50)
    print("测试 3: get_item_info")
    print("=" * 50)

    xianyu = XianyuApis()

    # 设置 cookie
    cookies_str = os.getenv("COOKIES_STR")
    cookies = trans_cookies(cookies_str)
    xianyu.session.cookies.update(cookies)

    # 测试参数
    item_id = "1003480140006"

    print(f"item_id: {item_id}")

    result = xianyu.get_item_info(item_id)

    print("\n返回结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1000] + "...")

    if 'data' in result and 'itemDO' in result['data']:
        item_data = result['data']['itemDO']
        title = item_data.get('title', 'N/A')
        price = item_data.get('soldPrice', 0)
        print(f"\n✅ 成功！商品标题: {title}, 价格: ¥{price/100}")
        return True
    else:
        print(f"\n❌ 失败")
        return False


def main():
    print("闲鱼 API 测试工具")
    print("=" * 50)

    # 检查环境变量
    cookie = os.getenv("COOKIES_STR")
    if not cookie or cookie == "your_cookies_here":
        print("❌ 错误: 请先配置 .env 文件中的 COOKIES_STR")
        sys.exit(1)

    print("✅ 环境变量已配置")
    print()

    # 运行测试
    tests = [
        ("获取订单支付信息", test_get_order_payment_info),
        ("自动发货", test_auto_delivery),
    ]

    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"\n测试出错: {str(e)}")
            results[name] = False

    # 汇总结果
    print("\n" + "=" * 50)
    print("测试结果汇总")
    print("=" * 50)
    for name, result in results.items():
        if result is None:
            status = "⏭️ 跳过"
        elif result:
            status = "✅ 通过"
        else:
            status = "❌ 失败"
        print(f"{name}: {status}")


if __name__ == "__main__":
    main()