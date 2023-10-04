import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, Filters
from telegram.error import BadRequest
import datetime
import requests
import sqlite3
import threading
import yaml

# Function to read YAML configuration file
def read_yaml_config(filename):
    with open(filename, 'r') as file:
        return yaml.safe_load(file)

# Reading configuration from 'config.yml'
config = read_yaml_config('config.yml')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

owner_id = config['owner_id']
telegram_token = config.get('telegram_token', '')
btc_wallet_address = config.get('btc_wallet_address', '')

lock = threading.Lock()
pending_actions = {}

def create_database():
    with lock:
        conn = sqlite3.connect('user_profiles.db')
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, notifications_enabled BOOLEAN, btc_wallet TEXT)")  # Cambio aquí
        cursor.execute("CREATE TABLE IF NOT EXISTS admins (admin_id INTEGER PRIMARY KEY)")
        cursor.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL, file TEXT)")
        conn.commit()
        conn.close()
        
def db_action(query, params=()):
    with lock:
        conn = sqlite3.connect('user_profiles.db')
        cursor = conn.cursor()
        cursor.execute(query, params)
        result = None
        if query.startswith("SELECT"):
            result = cursor.fetchall()
        else:
            conn.commit()
        conn.close()
        return result

create_database()

def create_user_profile(user_id, username):
        db_action("INSERT OR IGNORE INTO profiles (user_id, username, balance, notifications_enabled, btc_wallet) VALUES (?, ?, 0, 1, NULL)", (user_id, username))  # Cambio aquí

def add_admin_to_db(new_admin_id):
    db_action("INSERT OR IGNORE INTO admins (admin_id) VALUES (?)", (new_admin_id,))

def fetch_admin_ids():
    return [row[0] for row in db_action("SELECT admin_id FROM admins", ())]

admin_ids = fetch_admin_ids()

def is_admin(user_id):
    return user_id in admin_ids or user_id == owner_id

def add_product_to_db(name, price, file):
    db_action("INSERT INTO products (name, price, file) VALUES (?, ?, ?)", (name, price, file))

def fetch_products():
    return db_action("SELECT id, name, price FROM products", ())

def view_shop(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    products = fetch_products()
    if products:
        keyboard = [[InlineKeyboardButton(f"{name} - ${price}", callback_data=f'buy_{id}') for id, name, price in products]]
        keyboard.append([InlineKeyboardButton(config['buttons']['back_to_main_menu'], callback_data='main_menu')])
        markup = InlineKeyboardMarkup(keyboard)
        
        if query.message.text != config['messages']['shop_title'] or query.message.reply_markup != markup:
            query.edit_message_text(config['messages']['shop_title'], reply_markup=markup)
    else:
        keyboard = [[InlineKeyboardButton(config['buttons']['back_to_main_menu'], callback_data='main_menu')]]
        markup = InlineKeyboardMarkup(keyboard)
        if query.message.text != config['messages']['no_products_available']:
            query.edit_message_text(config['messages']['no_products_available'], reply_markup=markup)



def add_product(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    if is_admin(user_id):
        sent_message = query.message.reply_text(config['messages']['add_product'])
        pending_actions[user_id] = {'action': 'add_product_name', 'messages_to_delete': [sent_message.message_id]}

def handle_product_addition(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat_id
    action_data = pending_actions.get(user_id)
    if action_data:
        messages_to_delete = action_data.get('messages_to_delete', [])
        messages_to_delete.append(update.message.message_id)
        
        if action_data['action'] == 'add_product_name':
            product_name = update.message.text
            sent_message = update.message.reply_text(config['messages']['add_product_price'])
            messages_to_delete.append(sent_message.message_id)
            pending_actions[user_id] = {'action': 'add_product_price', 'name': product_name, 'messages_to_delete': messages_to_delete}
            
        elif action_data['action'] == 'add_product_price':
            product_price = float(update.message.text)
            sent_message = update.message.reply_text(config['messages']['add_product_file'])
            messages_to_delete.append(sent_message.message_id)
            pending_actions[user_id] = {'action': 'add_product_file', 'name': action_data['name'], 'price': product_price, 'messages_to_delete': messages_to_delete}
            
        elif action_data['action'] == 'add_product_file':
            if update.message.document:
                if update.message.document.mime_type == 'text/plain':
                    product_file = update.message.document.file_id
                    add_product_to_db(action_data['name'], action_data['price'], product_file)
                    sent_message = update.message.reply_text(config['messages']['product_added'])
                    
                    # Delete all previous messages
                    for msg_id in messages_to_delete:
                        try:
                            context.bot.delete_message(chat_id=user_id, message_id=msg_id)
                        except BadRequest:
                            pass
                    try:
                        context.bot.delete_message(chat_id=user_id, message_id=sent_message.message_id)
                    except BadRequest:
                        pass
                    
                    show_admin_panel(Update(effective_message=update.message, update_id=0), context)
                    
                    del pending_actions[user_id]
                else:
                    update.message.reply_text(config['messages']['invalid_file_type'])
            else:
                update.message.reply_text(config['messages']['no_document_attached'])
            
def buy_product(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    product_id = int(query.data.split('_')[1])
    product_data = db_action("SELECT name, price, file FROM products WHERE id = ?", (product_id,))
    if not product_data:
        query.answer(config['messages']['this_product_is_no_longer_available'])
        view_shop(update, context) 
        return
    
    product_name, product_price, file_id = product_data[0]
    user_balance_data = db_action("SELECT balance FROM profiles WHERE user_id = ?", (user_id,))
    if not user_balance_data:
        query.answer(config['messages']['an_error_occurred'])
        return
    
    user_balance = user_balance_data[0][0]
    
    if user_balance >= product_price:
        confirm_keyboard = [
            [InlineKeyboardButton(config['buttons']['confirm_purchase'], callback_data=f'confirm_{product_id}')],
            [InlineKeyboardButton(config['buttons']['cancel_purchase'], callback_data='view_shop')]
        ]
        confirm_markup = InlineKeyboardMarkup(confirm_keyboard)
        query.edit_message_text(f"Do you want to buy {product_name} for ${product_price}?", reply_markup=confirm_markup)
    else:
        query.answer(config['messages']['insufficient_balance'])
        view_shop(update, context)
        
def confirm_purchase(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    product_id = int(query.data.split('_')[1])
    product_data = db_action("SELECT name, price, file FROM products WHERE id = ?", (product_id,))
    if not product_data:
        query.answer(config['messages']['this_product_is_no_longer_available'])
        view_shop(update, context)
        return

    product_name, product_price, file_id = product_data[0]
    user_balance_data = db_action("SELECT balance FROM profiles WHERE user_id = ?", (user_id,))
    user_balance = user_balance_data[0][0]
    new_balance = user_balance - product_price

    db_action("UPDATE profiles SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    db_action("DELETE FROM products WHERE id = ?", (product_id,))
    query.answer(config['messages']['product_purchased_successfully'].format(product_name=product_name, new_balance=new_balance))
    context.bot.send_document(chat_id=user_id, document=file_id)
    view_shop(update, context)

def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat_id
    username = update.message.from_user.username if update.message.from_user.username else "Guest"
    exists = db_action("SELECT 1 FROM profiles WHERE user_id = ?", (user_id,))
    if not exists:
        create_user_profile(user_id, username)
    welcome_message = config['messages']['welcome_to_bot'].format(username=username)
    keyboard = [
        [InlineKeyboardButton(config['buttons']['view_profile'], callback_data='view_profile'), InlineKeyboardButton(config['buttons']['view_shop'], callback_data='view_shop')]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(welcome_message, reply_markup=markup)

def show_profile(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    profile = db_action("SELECT username, balance, notifications_enabled, btc_wallet FROM profiles WHERE user_id = ?", (user_id,))
    if profile:
        username, balance, notifications_enabled, btc_wallet = profile[0]
        profile_info = config['messages']['profile_info'].format(username=username, balance=balance)
        if btc_wallet:
            profile_info += f"\nBTC Wallet: {btc_wallet}"
        keyboard = [
            [InlineKeyboardButton(config['buttons']['main_menu'], callback_data='main_menu')],
            [InlineKeyboardButton(config['buttons']['disable_notifications'] if notifications_enabled else config['buttons']['enable_notifications'], callback_data='toggle_notifications')]
        ]
        if btc_wallet:
            keyboard.insert(0, [InlineKeyboardButton(config['buttons']['deposit'], callback_data='deposit')])
        if not btc_wallet:
            keyboard.append([InlineKeyboardButton(config['buttons']['add_wallet_address'], callback_data='add_wallet_address')])
        if is_admin(user_id):
            keyboard.append([InlineKeyboardButton(config['buttons']['admin_panel'], callback_data='admin_panel')])
        markup = InlineKeyboardMarkup(keyboard)
        query.edit_message_text(profile_info, reply_markup=markup)
    else:
        query.edit_message_text(config['messages']['error_fetching_profile'])

def show_admin_panel(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    if is_admin(user_id):
        admin_keyboard = [
            [InlineKeyboardButton(config['buttons']['add_product'], callback_data='add_product')],
            [InlineKeyboardButton(config['buttons']['add_new_admin'], callback_data='add_new_admin')],
            [InlineKeyboardButton(config['buttons']['back_to_profile'], callback_data='view_profile')]
        ]
        if user_id == config['owner_id']:
            admin_keyboard.append([InlineKeyboardButton(config['buttons']['show_admin_list'], callback_data='show_admin_list')])
        admin_markup = InlineKeyboardMarkup(admin_keyboard)
        query.edit_message_text(config['messages']['admin_panel_title'], reply_markup=admin_markup)
    else:
        query.answer(config['messages']['you_do_not_have_permission_to_access'])


def back_to_profile(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    show_profile(update, context)

def toggle_notifications(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    profile = db_action("SELECT notifications_enabled FROM profiles WHERE user_id = ?", (user_id,))
    if profile:
        notifications_enabled, = profile[0]
        new_status = not notifications_enabled
        db_action("UPDATE profiles SET notifications_enabled = ? WHERE user_id = ?", (new_status, user_id))
        query.answer(config['messages']['notifications_status'].format(status='enabled' if new_status else 'disabled'))
        show_profile(update, context)
    else:
        query.answer(config['messages']['an_error_occurred'])


def back_to_main_menu(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    profile = db_action("SELECT username FROM profiles WHERE user_id = ?", (user_id,))
    if profile:
        username, = profile[0]
        welcome_message = config['messages']['welcome_back_message'].format(username=username)
        keyboard = [
            [InlineKeyboardButton(config['buttons']['view_profile'], callback_data='view_profile'), InlineKeyboardButton(config['buttons']['view_shop'], callback_data='view_shop')]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        query.edit_message_text(welcome_message, reply_markup=markup)
    else:
        query.edit_message_text(config['messages']['error_fetching_profile'])


def add_new_admin(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    if is_admin(user_id):
        sent_message = query.message.reply_text(config['messages']['please_send_new_admin_id'])
        pending_actions[user_id] = {'action': 'add_new_admin', 'messages_to_delete': [sent_message.message_id]}
    else:
        query.answer(config['messages']['you_do_not_have_permission_to_add_admin'])

def add_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat_id
    action_data = pending_actions.get(user_id)
    if action_data and action_data['action'] == 'add_new_admin':
        new_admin_id = int(update.message.text)
        add_admin_to_db(new_admin_id)
        refresh_admin_ids()  # Assuming this function refreshes the admin_ids global variable
        update.message.reply_text(config['messages']['admin_added'].format(new_admin_id=new_admin_id))
        del pending_actions[user_id]
    else:
        update.message.reply_text(config['messages']['you_do_not_have_permission_to_add_admin'])

def revoke_admin(admin_id):
    db_action("DELETE FROM admins WHERE admin_id = ?", (admin_id,))

def refresh_admin_ids():
    global admin_ids
    admin_ids = fetch_admin_ids()

def show_admin_list(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    if user_id == owner_id:
        admin_list = fetch_admin_ids()
        keyboard = [[InlineKeyboardButton(str(admin_id), callback_data=f'revoke_{admin_id}')] for admin_id in admin_list]
        keyboard.append([InlineKeyboardButton(config['buttons']['back_to_admin_panel'], callback_data='admin_panel')])
        markup = InlineKeyboardMarkup(keyboard)
        query.edit_message_text(config['messages']['admin_list_title'], reply_markup=markup)


def revoke_admin_permission(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    if user_id == owner_id:
        admin_id_to_revoke = int(query.data.split('_')[1])
        revoke_admin(admin_id_to_revoke)
        refresh_admin_ids()
        query.answer(config['messages']['revoked_admin_permission'].format(admin_id_to_revoke=admin_id_to_revoke))
        show_admin_list(update, context)

def handle_pending_actions(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat_id
    action_data = pending_actions.get(user_id)
    
    if action_data:
        action = action_data['action']
        
        if action == 'add_product_name' or action == 'add_product_price' or action == 'add_product_file':
            handle_product_addition(update, context)
        elif action == 'add_new_admin':
            add_admin(update, context)

def add_wallet_address(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    sent_message = query.message.reply_text(config['messages']['please_send_btc_wallet'])
    pending_actions[user_id] = {'action': 'add_wallet_address', 'messages_to_delete': [sent_message.message_id]}


def handle_pending_actions(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat_id
    action_data = pending_actions.get(user_id)
    if action_data:
        action = action_data['action']
        if action == 'add_wallet_address':
            btc_wallet = update.message.text
            db_action("UPDATE profiles SET btc_wallet = ? WHERE user_id = ?", (btc_wallet, user_id))
            del pending_actions[user_id]
            update.message.reply_text(config['messages']['btc_wallet_added'])
            show_profile(Update(effective_message=update.message, update_id=0), context)


btc_wallet_address = config.get('btc_wallet_address', '')

def show_deposit_menu(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    deposit_message = config['messages']['deposit_message'].format(btc_wallet_address=btc_wallet_address)
    keyboard = [
        [InlineKeyboardButton(config['buttons']['verify_transaction'], callback_data='verify_transaction')],
        [InlineKeyboardButton(config['buttons']['back_to_profile'], callback_data='view_profile')]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    query.edit_message_text(deposit_message, reply_markup=markup)


def verify_transaction(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat_id
    user_data = db_action("SELECT btc_wallet FROM profiles WHERE user_id = ?", (user_id,))
    if user_data:
        user_wallet = user_data[0][0]
        amount_in_btc = check_btc_transaction(user_wallet, btc_wallet_address)
        if amount_in_btc:
            btc_to_usd_rate = 27000
            amount_in_usd = amount_in_btc * btc_to_usd_rate
            
            db_action("UPDATE profiles SET balance = balance + ? WHERE user_id = ?", (amount_in_usd, user_id))
            query.answer(config['messages']['transaction_verified'].format(amount_in_usd=amount_in_usd))
        else:
            query.answer("No transaction found in the last hour.")
    else:
        query.answer("An error occurred.")
    show_profile(update, context)  


verified_tx_hashes = set()

def check_btc_transaction(user_wallet, destination_wallet):
    global verified_tx_hashes  
    url = f"https://blockchain.info/rawaddr/{user_wallet}"
    response = requests.get(url)
    if response.status_code == 200:
        transactions = response.json().get("txs", [])
        
        current_time = datetime.datetime.now()
        one_hour_ago = current_time - datetime.timedelta(hours=1)
        
        for tx in transactions:
            tx_hash = tx['hash']
            
            if tx_hash in verified_tx_hashes:
                continue
                
            time = datetime.datetime.fromtimestamp(tx['time'])
            if time > one_hour_ago:
                for out in tx['out']:
                    if out['addr'] == destination_wallet:
                        amount = out['value'] 
                        amount_in_btc = amount / 1e8  
                        
                        verified_tx_hashes.add(tx_hash)
                        
                        return amount_in_btc 
    return None

def main() -> None:
    updater = Updater(telegram_token)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command | Filters.document.mime_type("text/plain") & ~Filters.command, handle_pending_actions))
    dp.add_handler(CallbackQueryHandler(buy_product, pattern='^buy_\\d+$'))
    dp.add_handler(CallbackQueryHandler(view_shop, pattern='^view_shop$'))
    dp.add_handler(CallbackQueryHandler(add_product, pattern='^add_product$'))
    dp.add_handler(CallbackQueryHandler(confirm_purchase, pattern='^confirm_\\d+$'))
    dp.add_handler(CallbackQueryHandler(show_deposit_menu, pattern='^deposit$'))
    dp.add_handler(CallbackQueryHandler(verify_transaction, pattern='^verify_transaction$'))
    dp.add_handler(CallbackQueryHandler(add_wallet_address, pattern='^add_wallet_address$'))
    dp.add_handler(CallbackQueryHandler(show_profile, pattern='^view_profile$'))
    dp.add_handler(CallbackQueryHandler(show_admin_panel, pattern='^admin_panel$'))
    dp.add_handler(CallbackQueryHandler(add_new_admin, pattern='^add_new_admin$'))
    dp.add_handler(CallbackQueryHandler(back_to_profile, pattern='^back_to_profile$'))
    dp.add_handler(CallbackQueryHandler(toggle_notifications, pattern='^toggle_notifications$'))
    dp.add_handler(CallbackQueryHandler(back_to_main_menu, pattern='^main_menu$'))
    dp.add_handler(CallbackQueryHandler(show_admin_list, pattern='^show_admin_list$'))
    dp.add_handler(CallbackQueryHandler(revoke_admin_permission, pattern='^revoke_\\d+$'))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()