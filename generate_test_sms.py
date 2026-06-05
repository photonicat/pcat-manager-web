#!/usr/bin/env python3
"""
SMS Test Data Generator
Generates 30-100 random SMS messages for testing the SMS interface.
Includes both sent and received messages with various phone numbers and content.
"""

import sqlite3
import random
import datetime
import hashlib
import sys
import os

# Add the app directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

# Sample phone numbers
PHONE_NUMBERS = [
    "+86 138 0013 8000",
    "+86 139 0013 9000", 
    "+1 555 123 4567",
    "+1 555 987 6543",
    "+86 151 1234 5678",
    "+86 152 9876 5432",
    "+1 555 555 0123",
    "+44 20 7123 4567",
    "+33 1 23 45 67 89",
    "10001"  # Service number
]

# Sample message content (mix of Chinese and English)
SAMPLE_MESSAGES = [
    # Common messages
    "Hello, how are you?",
    "你好吗？",
    "Meeting at 3pm today",
    "今天下午3点开会",
    "Thanks for your help",
    "谢谢你的帮助",
    "Can you call me back?",
    "你能回电话给我吗？",
    "Running late, will be there in 10 mins",
    "我要迟到了，10分钟后到",
    
    # Service messages
    "Your package has been delivered",
    "您的快递已送达",
    "验证码：123456，5分钟内有效",
    "Verification code: 123456, valid for 5 minutes",
    "Your account balance is $25.50",
    "您的账户余额为25.50元",
    "System maintenance tonight 12AM-2AM",
    "系统维护时间：今晚12点-2点",
    
    # Conversation messages
    "Sure, let's do it",
    "好的，我们这样做",
    "What time works for you?",
    "什么时间对你合适？",
    "See you tomorrow",
    "明天见",
    "OK",
    "好的",
    "👍",
    "😊",
    
    # Longer messages
    "Hey, just wanted to check if you're still available for dinner tonight. Let me know!",
    "嗨，想确认一下你今晚是否还有空吃晚饭。让我知道！",
    "The meeting has been moved to conference room B on the 5th floor. Please bring your laptop.",
    "会议已改到5楼B会议室。请带上你的笔记本电脑。",
    "Happy birthday! Hope you have a wonderful day filled with joy and laughter! 🎂🎉",
    "生日快乐！希望你度过充满欢乐和笑声的美好一天！🎂🎉"
]

def generate_random_timestamp(days_back=30):
    """Generate a random timestamp within the last N days"""
    now = datetime.datetime.now()
    random_days = random.uniform(0, days_back)
    random_hours = random.uniform(0, 24)
    random_minutes = random.uniform(0, 60)
    
    timestamp = now - datetime.timedelta(
        days=random_days,
        hours=random_hours,
        minutes=random_minutes
    )
    return timestamp.strftime('%Y-%m-%d %H:%M:%S')

def generate_sms_messages(count):
    """Generate a list of SMS messages"""
    messages = []
    
    for i in range(count):
        # Random direction (0 = received, 1 = sent)
        direction = random.choice([0, 1])
        
        # Select phone number (some repeated for conversations)
        phone_number = random.choice(PHONE_NUMBERS)
        
        # Select message content
        content = random.choice(SAMPLE_MESSAGES)
        
        # Generate timestamp
        timestamp = generate_random_timestamp(30)
        
        # Create hash for message
        hash_input = f"{phone_number}:{content}:{timestamp}"
        hash_digest = hashlib.md5(hash_input.encode()).hexdigest()
        
        message = {
            'id': i + 1,
            'from_num': phone_number if direction == 0 else '',
            'to_num': phone_number if direction == 1 else '',
            'content': content,
            'direction': direction,
            'timestamp': timestamp,
            'created_at': timestamp,
            'sent_at': timestamp if direction == 1 else None,
            'status': 'SENT' if direction == 1 else 'RECEIVED',
            'hash_digest': hash_digest,
            'raw_json': None
        }
        messages.append(message)
    
    # Sort messages by timestamp
    messages.sort(key=lambda x: x['timestamp'])
    
    return messages

def insert_messages_to_db(messages, db_path='/etc/pc_modem.db'):
    """Insert messages into the SMS database"""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create table if it doesn't exist
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_num TEXT,
            to_num TEXT,
            content TEXT,
            direction INTEGER,
            timestamp TEXT,
            created_at TEXT,
            sent_at TEXT,
            status TEXT,
            hash_digest TEXT,
            raw_json TEXT
        )
        ''')
        
        # Clear existing test data (optional)
        cursor.execute('DELETE FROM sms')
        
        # Insert new messages
        for msg in messages:
            cursor.execute('''
            INSERT INTO sms (from_num, to_num, content, direction, timestamp, created_at, sent_at, status, hash_digest, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                msg['from_num'], msg['to_num'], msg['content'], msg['direction'],
                msg['timestamp'], msg['created_at'], msg['sent_at'], msg['status'],
                msg['hash_digest'], msg['raw_json']
            ))
        
        conn.commit()
        conn.close()
        
        print(f"Successfully inserted {len(messages)} messages into database at {db_path}")
        return True
        
    except Exception as e:
        print(f"Error inserting messages into database: {e}")
        return False

def main():
    """Main function"""
    # Generate random number of messages between 30-100
    message_count = random.randint(30, 100)
    
    print(f"Generating {message_count} test SMS messages...")
    
    # Generate messages
    messages = generate_sms_messages(message_count)
    
    # Print summary
    sent_count = sum(1 for msg in messages if msg['direction'] == 1)
    received_count = len(messages) - sent_count
    unique_numbers = len(set([msg['from_num'] or msg['to_num'] for msg in messages]))
    
    print(f"\nGenerated Messages Summary:")
    print(f"  Total messages: {len(messages)}")
    print(f"  Sent messages: {sent_count}")
    print(f"  Received messages: {received_count}")
    print(f"  Unique phone numbers: {unique_numbers}")
    
    # Ask user if they want to insert into database
    response = input(f"\nInsert messages into database? (y/N): ").strip().lower()
    
    if response in ['y', 'yes']:
        # Check for custom database path
        db_path = input("Database path (press Enter for default): ").strip()
        if not db_path:
            db_path = '/etc/pc_modem.db'
        
        success = insert_messages_to_db(messages, db_path)
        if success:
            print(f"\n✅ Test data generated successfully!")
            print(f"   You can now refresh the SMS interface to see the test messages.")
        else:
            print(f"\n❌ Failed to insert test data into database.")
    else:
        print("\nTest messages generated but not inserted into database.")
        print("Sample messages:")
        for i, msg in enumerate(messages[:5]):
            direction = "Sent" if msg['direction'] == 1 else "Received"
            phone = msg['to_num'] if msg['direction'] == 1 else msg['from_num']
            print(f"  {i+1}. [{direction}] {phone}: {msg['content'][:50]}...")

if __name__ == "__main__":
    main()