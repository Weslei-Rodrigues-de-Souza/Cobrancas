# app.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash, Blueprint, jsonify
from datetime import datetime, timedelta, date
from database import db, EmailLog, Cliente, Boleto, Contato, ConfiguracaoEmail # Importação da classe EmailLog
from database import generate_uuid # Importando generate_uuid
from dateutil.relativedelta import relativedelta
import uuid 
import logging
from collections import defaultdict
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, desc
import socket 

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage 
from email.utils import formataddr, parseaddr
import re 
import base64 

# Função para formatar o horário para exibição no template
def formatar_horario(dt_obj):
    if isinstance(dt_obj, datetime):
        return dt_obj.strftime('%d/%m/%Y %H:%M:%S')
    # Retorna N/D se não for um objeto datetime ou se for None
    return 'N/D'


app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'uma_chave_secreta_muito_forte_padrao')

if not app.debug:
    app.logger.setLevel(logging.INFO)
    if not app.logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
else:
    app.logger.setLevel(logging.DEBUG)
    if not app.logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        # Adicionar um FileHandler em DEBUG para log mais detalhado
        file_handler = logging.FileHandler('app_debug.log')
        file_handler.setFormatter(formatter)
        app.logger.addHandler(handler)


if 'SQLALCHEMY_DATABASE_URI' not in app.config:
    base_dir_for_db = os.path.abspath(os.path.dirname(__file__))
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(base_dir_for_db, 'cobrancas.db')
    # INICIALIZAÇÃO DO SQLAlchemy
    db.init_app(app)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError 
import atexit 
jobstores = { 'default': SQLAlchemyJobStore(url=app.config['SQLALCHEMY_DATABASE_URI']) }
scheduler = BackgroundScheduler(jobstores=jobstores, timezone='America/Sao_Paulo') # Definindo o timezone do scheduler

# Registrar o filtro personalizado no ambiente Jinja2
app.jinja_env.filters['formatar_horario'] = formatar_horario

main_bp = Blueprint('main', __name__)

# --- Rotas da Aplicação (index, clientes, boletos, etc.) ---
# (COLE AQUI TODAS AS SUAS ROTAS EXISTENTES, COMO NA VERSÃO ANTERIOR)
# ... (O código das rotas de / a /boletos/<public_id>/dados_json permanece o mesmo da versão app_py_gmail_direct_final_fixes_v2) ...
@main_bp.route('/')
def index():
    total_clientes = Cliente.query.count()
    valor_boletos_aberto = db.session.query(func.sum(Boleto.valor)).filter(Boleto.status == 'pendente').scalar() or 0.0
    valor_boletos_pagos = db.session.query(func.sum(Boleto.valor)).filter(Boleto.status == 'pago').scalar() or 0.0
    cliente_reincidente_data = db.session.query(
        Cliente.nome,
        func.count(func.distinct(Boleto.grupo_replicacao_id)).label('num_cobrancas_distintas')
    ).join(Boleto, Cliente.id == Boleto.cliente_id)\
     .group_by(Cliente.id, Cliente.nome)\
     .order_by(desc('num_cobrancas_distintas'))\
     .first()
    cliente_mais_reincidente_nome = "N/D"
    num_cobrancas_reincidente = 0
    if cliente_reincidente_data:
        cliente_mais_reincidente_nome = cliente_reincidente_data[0]
        num_cobrancas_reincidente = cliente_reincidente_data[1]
    total_boletos_pendentes_contagem = Boleto.query.filter_by(status='pendente').count()
    return render_template('index.html',
                           total_clientes=total_clientes,
                           total_boletos_pendentes=total_boletos_pendentes_contagem,
                           valor_boletos_aberto=valor_boletos_aberto,
                           valor_boletos_pagos=valor_boletos_pagos,
                           cliente_mais_reincidente_nome=cliente_mais_reincidente_nome,
                           num_cobrancas_reincidente=num_cobrancas_reincidente)

@main_bp.route('/clientes')
def listar_clientes():
    search_term = request.args.get('q_cliente', '')
    query = Cliente.query.order_by(Cliente.nome)
    if search_term:
        query = query.filter(Cliente.nome.ilike(f'%{search_term}%'))
    todos_os_clientes = query.all()
    return render_template('clientes.html',
                           lista_completa_clientes=todos_os_clientes,
                           search_term_cliente=search_term)

@main_bp.route('/clientes/novo', methods=['GET', 'POST'])
def novo_cliente():
    if request.method == 'POST':
        nome_empresa = request.form.get('nome_empresa')
        contato_nome = request.form.get('contato_nome')
        contato_email = request.form.get('contato_email')
        contato_telefone = request.form.get('contato_telefone')

        if not nome_empresa:
            flash('Razão Social / Nome Fantasia é obrigatório.', 'warning')
            return render_template('form_cliente.html', form_data=request.form, edit_mode=False)

        criar_contato_principal_automaticamente = bool(contato_nome and contato_email) 
        
        cliente_email_para_db = None
        cliente_telefone_para_db = None

        if criar_contato_principal_automaticamente:
            cliente_email_para_db = contato_email
            cliente_telefone_para_db = contato_telefone
            if contato_email:
                cliente_existente_com_email = Cliente.query.filter_by(email_principal=contato_email).first()
                if cliente_existente_com_email:
                    flash(f'Erro: O e-mail "{contato_email}" fornecido para o contato já está em uso como e-mail principal por outro cliente. Por favor, use um e-mail diferente ou edite o cliente existente.', 'danger')
                    return render_template('form_cliente.html', form_data=request.form, edit_mode=False)
        
        try:
            novo_cliente_obj = Cliente(
                nome=nome_empresa,
                public_id=generate_uuid(), 
                email_principal=cliente_email_para_db,
                telefone_principal=cliente_telefone_para_db
            )
            db.session.add(novo_cliente_obj)
            db.session.flush() 

            if criar_contato_principal_automaticamente:
                contato_obj = Contato(
                    nome=contato_nome,
                    email=contato_email,
                    telefone=contato_telefone,
                    cliente_id=novo_cliente_obj.id, 
                    is_principal=True
                )
                db.session.add(contato_obj)
                flash_message = f'Cliente "{nome_empresa}" e contato principal "{contato_nome}" cadastrados com sucesso!'
            else:
                flash_message = f'Cliente "{nome_empresa}" cadastrado. É recomendável adicionar um contato principal.'
            
            db.session.commit() 
            db.session.refresh(novo_cliente_obj)
            
            if not novo_cliente_obj.public_id: 
                app.logger.error(f"CRÍTICO: public_id para cliente '{nome_empresa}' é None APÓS commit e refresh.")
                flash('Erro crítico ao salvar cliente (ID público não gerado). Contacte o suporte.', 'danger')
                return redirect(url_for('main.listar_clientes')) 

            flash(flash_message, 'success')
            return redirect(url_for('main.view_cliente', public_id=novo_cliente_obj.public_id))

        except IntegrityError as e:
            db.session.rollback()
            app.logger.error(f"Erro de integridade ao cadastrar cliente: {e}", exc_info=True)
            if 'UNIQUE constraint failed: cliente.email_principal' in str(e) and cliente_email_para_db:
                 flash(f'Erro: O e-mail principal "{cliente_email_para_db}" já está em uso por outro cliente.', 'danger')
            elif 'UNIQUE constraint failed: cliente.public_id' in str(e):
                 flash('Erro: Falha ao gerar um ID público único para o cliente. Tente novamente.', 'danger')
            else:
                flash('Erro de integridade ao salvar os dados.', 'danger')
        except Exception as e: 
            db.session.rollback() 
            app.logger.error(f"Erro inesperado ao cadastrar cliente: {e}", exc_info=True)
            flash(f'Erro inesperado ao processar o cadastro: {str(e)}', 'danger')
        
        return render_template('form_cliente.html', form_data=request.form, edit_mode=False)
    return render_template('form_cliente.html', form_data={}, edit_mode=False)

@main_bp.route('/clientes/<string:public_id>')
def view_cliente(public_id):
    cliente = Cliente.query.filter_by(public_id=public_id).first_or_404()
    boletos_agrupados_dict = defaultdict(list)
    boletos_do_cliente_ordenados = sorted(
        [b for b in cliente.boletos],
        key=lambda b: (b.grupo_replicacao_id if b.grupo_replicacao_id else str(uuid.uuid4()), 
                       b.data_vencimento if b.data_vencimento else date.min, 
                       b.parcela_atual)
    )
    for boleto_item in boletos_do_cliente_ordenados:
        grupo_chave = boleto_item.grupo_replicacao_id if boleto_item.grupo_replicacao_id else boleto_item.public_id
        boletos_agrupados_dict[grupo_chave].append(boleto_item)
    
    lista_de_grupos_de_boletos = []
    for grupo_id, boletos_no_grupo in boletos_agrupados_dict.items():
        boletos_no_grupo_ordenados = sorted(boletos_no_grupo, key=lambda b: (b.data_vencimento if b.data_vencimento else date.min, b.parcela_atual))
        if boletos_no_grupo_ordenados:
            lista_de_grupos_de_boletos.append({
                'id_do_grupo': grupo_id, 'boletos': boletos_no_grupo_ordenados,
                'e_serie': len(boletos_no_grupo_ordenados) > 1 and boletos_no_grupo_ordenados[0].grupo_replicacao_id is not None,
                'primeiro_boleto': boletos_no_grupo_ordenados[0]
            })
    lista_de_grupos_de_boletos.sort(key=lambda g: (g['primeiro_boleto'].data_vencimento if g['primeiro_boleto'].data_vencimento else date.min))
    return render_template('view_cliente.html', cliente=cliente, boletos_agrupados_lista=lista_de_grupos_de_boletos)

@main_bp.route('/clientes/<string:public_id>/editar', methods=['GET', 'POST'])
def editar_cliente(public_id):
    cliente = Cliente.query.filter_by(public_id=public_id).first_or_404()
    if request.method == 'POST':
        nome_empresa = request.form.get('nome_empresa')
        if not nome_empresa:
            flash('Razão Social / Nome Fantasia é obrigatório.', 'warning')
            return render_template('form_cliente.html', cliente=cliente, form_data=request.form, edit_mode=True, public_id=cliente.public_id)
        
        cliente.nome = nome_empresa
        contato_principal_atual = Contato.query.filter_by(cliente_id=cliente.id, is_principal=True).first()
        
        novo_email_para_cliente = contato_principal_atual.email if contato_principal_atual else None
        novo_telefone_para_cliente = contato_principal_atual.telefone if contato_principal_atual else None

        if novo_email_para_cliente and cliente.email_principal != novo_email_para_cliente:
            outro_cliente_com_mesmo_email = Cliente.query.filter(
                Cliente.email_principal == novo_email_para_cliente,
                Cliente.id != cliente.id
            ).first()
            if outro_cliente_com_mesmo_email:
                flash(f'Erro: O e-mail "{novo_email_para_cliente}" (do contato principal) já está em uso por outro cliente.', 'danger')
                return render_template('form_cliente.html', cliente=cliente, form_data=request.form, edit_mode=True, public_id=cliente.public_id)
        
        cliente.email_principal = novo_email_para_cliente
        cliente.telefone_principal = novo_telefone_para_cliente
        
        try:
            db.session.commit()
            flash(f'Cliente "{cliente.nome}" atualizado com sucesso!', 'success')
            return redirect(url_for('main.view_cliente', public_id=cliente.public_id))
        except IntegrityError as e: 
            db.session.rollback()
            if 'UNIQUE constraint failed: cliente.email_principal' in str(e) and novo_email_para_cliente:
                 flash(f'Erro: O e-mail principal "{novo_email_para_cliente}" já está em uso.', 'danger')
            else:
                flash('Erro de integridade ao salvar as alterações do cliente.', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao atualizar cliente: {str(e)}', 'danger')
        return render_template('form_cliente.html', cliente=cliente, form_data=request.form, edit_mode=True, public_id=cliente.public_id)
    return render_template('form_cliente.html', cliente=cliente, form_data={'nome_empresa': cliente.nome}, edit_mode=True, public_id=cliente.public_id)

@main_bp.route('/clientes/<string:public_id>/excluir', methods=['POST'])
def excluir_cliente(public_id):
    cliente = Cliente.query.filter_by(public_id=public_id).first_or_404()
    nome_cliente = cliente.nome 
    try:
        db.session.delete(cliente)
        db.session.commit()
        flash(f'Cliente "{nome_cliente}" e todos os seus dados foram excluídos.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao excluir cliente: {str(e)}', 'danger')
    return redirect(url_for('main.listar_clientes'))

@main_bp.route('/clientes/<string:public_id>/contatos/novo', methods=['POST'])
def novo_contato(public_id): 
    cliente = Cliente.query.filter_by(public_id=public_id).first_or_404()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.method == 'POST':
        nome_contato = request.form.get('nome_contato')
        email_contato = request.form.get('email_contato')
        telefone_contato = request.form.get('telefone_contato')
        is_principal_submetido = request.form.get('is_principal') == 'true'
        
        erros = {}
        if not nome_contato: 
            erros['nome_contato'] = 'Nome do contato é obrigatório.'
        
        if erros:
            if is_ajax:
                return jsonify(success=False, errors=erros, message="Por favor, corrija os erros abaixo."), 400 
            else:
                for _, msg in erros.items(): flash(msg, 'warning')
                return redirect(url_for('main.view_cliente', public_id=public_id))
        
        try:
            if is_principal_submetido:
                contatos_principais_anteriores = Contato.query.filter_by(cliente_id=cliente.id, is_principal=True).all()
                for cp_antigo in contatos_principais_anteriores:
                    cp_antigo.is_principal = False
                
                if email_contato: 
                    outro_cliente_com_email = Cliente.query.filter(Cliente.email_principal == email_contato, Cliente.id != cliente.id).first()
                    if outro_cliente_com_mesmo_email:
                        msg_erro_email_duplicado = f'Erro: E-mail "{email_contato}" já em uso como e-mail principal por outro cliente.'
                        if is_ajax: return jsonify(success=False, message=msg_erro_email_duplicado, errors={'email_contato': msg_erro_email_duplicado}), 400
                        else:
                            flash(msg_erro_email_duplicado, 'danger')
                            db.session.rollback()
                            return redirect(url_for('main.view_cliente', public_id=public_id))
                    cliente.email_principal = email_contato
                else: 
                    cliente.email_principal = None 
                cliente.telefone_principal = telefone_contato
            
            novo_contato_obj = Contato(
                nome=nome_contato, email=email_contato, telefone=telefone_contato,
                cliente_id=cliente.id, is_principal=is_principal_submetido
            )
            db.session.add(novo_contato_obj)
            db.session.commit() 
            
            cliente_info_updated = {
                'email_principal': cliente.email_principal or 'N/D (defina um contato principal)',
                'telefone_principal': cliente.telefone_principal or 'N/D (defina um contato principal)'
            }

            if is_ajax:
                return jsonify(success=True, message=f'Contato "{nome_contato}" adicionado!', 
                               contato={
                                   'id': novo_contato_obj.id, 
                                   'nome': novo_contato_obj.nome, 
                                   'email': novo_contato_obj.email or '-', 
                                   'telefone': novo_contato_obj.telefone or '-',
                                   'is_principal': novo_contato_obj.is_principal,
                                   'cliente_public_id': cliente.public_id 
                                   },
                               cliente_info=cliente_info_updated if is_principal_submetido else None
                               ), 200 
            else: 
                flash(f'Contato "{nome_contato}" adicionado!', 'success')
                return redirect(url_for('main.view_cliente', public_id=cliente.public_id))

        except IntegrityError as e:
            db.session.rollback()
            app.logger.error(f"Integridade ao adicionar contato: {e}", exc_info=True)
            msg_erro = 'Erro de integridade ao salvar o contato.'
            if 'UNIQUE constraint failed: cliente.email_principal' in str(e) and email_contato:
                 msg_erro = f'Erro: O e-mail "{email_contato}" já está em uso como e-mail principal por outro cliente.'
            if is_ajax: return jsonify(success=False, message=msg_erro, errors={'geral': msg_erro}), 400 
            else: flash(msg_erro, 'danger')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Erro ao adicionar contato para cliente {public_id}: {e}", exc_info=True)
            msg_erro_geral = f'Erro ao adicionar contato: {str(e)}'
            if is_ajax: return jsonify(success=False, message=msg_erro_geral, errors={'geral': msg_erro_geral}), 500
            else: flash(msg_erro_geral, 'danger')
        
        if not is_ajax: return redirect(url_for('main.view_cliente', public_id=public_id))
        return jsonify(success=False, message="Erro desconhecido no servidor ao adicionar contato."), 500
    
    if is_ajax: return jsonify(success=False, message="Método GET não suportado para esta ação AJAX."), 405
    return redirect(url_for('main.view_cliente', public_id=public_id))


@main_bp.route('/contatos/<int:contato_id>/editar', methods=['POST']) 
def editar_contato(contato_id):
    contato = db.session.get(Contato, contato_id) 
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not contato:
        if is_ajax: return jsonify(success=False, message="Contato não encontrado."), 404
        flash('Contato não encontrado.', 'danger')
        return redirect(url_for('main.listar_clientes')) 
    
    cliente = contato.cliente
    if not cliente: 
        if is_ajax: return jsonify(success=False, message="Cliente associado não encontrado."), 404
        flash('Cliente do contato não encontrado.', 'danger')
        return redirect(url_for('main.listar_clientes'))

    if request.method == 'POST':
        nome_contato = request.form.get('nome_contato')
        email_contato_novo = request.form.get('email_contato') 
        telefone_contato_novo = request.form.get('telefone_contato') 
        is_principal_submetido = request.form.get('is_principal') == 'true'

        erros = {}
        if not nome_contato:
            erros['nome_contato'] = 'Nome do contato é obrigatório.'
        
        if erros:
            if is_ajax: return jsonify(success=False, errors=erros, message="Por favor, corrija os erros abaixo."), 400
            else: 
                for _, msg in erros.items(): flash(msg, 'warning')
                return redirect(url_for('main.view_cliente', public_id=cliente.public_id))

        try:
            if is_principal_submetido:
                Contato.query.filter(
                    Contato.cliente_id == cliente.id,
                    Contato.id != contato.id, 
                    Contato.is_principal == True
                ).update({"is_principal": False})
                
                if email_contato_novo:
                    outro_cliente_com_email = Cliente.query.filter(Cliente.email_principal == email_contato_novo, Cliente.id != cliente.id).first()
                    if outro_cliente_com_mesmo_email:
                        msg_erro_email_duplicado = f'Erro: E-mail "{email_contato_novo}" já em uso como e-mail principal por outro cliente.'
                        if is_ajax: return jsonify(success=False, message=msg_erro_email_duplicado, errors={'email_contato': msg_erro_email_duplicado}), 400
                        else:
                            flash(msg_erro_email_duplicado, 'danger')
                            db.session.rollback()
                            return redirect(url_for('main.view_cliente', public_id=cliente.public_id))
                    cliente.email_principal = email_contato_novo
                else: 
                    cliente.email_principal = None
                cliente.telefone_principal = telefone_contato_novo
            elif not is_principal_submetido and contato.is_principal: 
                cliente.email_principal = None
                cliente.telefone_principal = None
            
            contato.nome = nome_contato
            contato.email = email_contato_novo
            contato.telefone = telefone_contato_novo
            contato.is_principal = is_principal_submetido
            
            db.session.commit()

            cliente_info_updated = {
                'email_principal': cliente.email_principal or 'N/D (defina um contato principal)',
                'telefone_principal': cliente.telefone_principal or 'N/D (defina um contato principal)'
            }
            
            if is_ajax:
                return jsonify(success=True, message=f'Contato "{contato.nome}" atualizado!',
                               contato={
                                   'id': contato.id, 
                                   'nome': contato.nome, 
                                   'email': contato.email or '-', 
                                   'telefone': contato.telefone or '-',
                                   'is_principal': contato.is_principal,
                                   'cliente_public_id': cliente.public_id
                                   },
                               cliente_info=cliente_info_updated
                               ), 200
            else: 
                flash(f'Contato "{contato.nome}" atualizado!', 'success')
                return redirect(url_for('main.view_cliente', public_id=cliente.public_id))

        except IntegrityError as e:
            db.session.rollback()
            app.logger.error(f"Integridade ao editar contato: {e}", exc_info=True)
            msg_erro = 'Erro de integridade ao salvar o contato.'
            if 'UNIQUE constraint failed: cliente.email_principal' in str(e) and email_contato_novo:
                 msg_erro = f'Erro: O e-mail "{email_contato_novo}" já está em uso como e-mail principal por outro cliente.'
            if is_ajax: return jsonify(success=False, message=msg_erro, errors={'geral': msg_erro}), 400
            else: flash(msg_erro, 'danger')
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Erro ao editar contato {contato_id}: {e}", exc_info=True)
            msg_erro_geral = f'Erro ao atualizar contato: {str(e)}'
            if is_ajax: return jsonify(success=False, message=msg_erro_geral, errors={'geral': msg_erro_geral}), 500
            else: flash(msg_erro_geral, 'danger')
        
        if not is_ajax: return redirect(url_for('main.view_cliente', public_id=cliente.public_id))
        return jsonify(success=False, message="Erro desconhecido no servidor ao editar contato."), 500

    if is_ajax: return jsonify(success=False, message="Método GET não suportado para esta ação AJAX."), 405
    return redirect(url_for('main.view_cliente', public_id=cliente.public_id)) # Fallback para GET


@main_bp.route('/contatos/<int:contato_id>/excluir', methods=['POST'])
def excluir_contato(contato_id):
    contato = db.session.get(Contato, contato_id)
    if not contato:
        flash('Contato não encontrado.', 'danger')
        return redirect(url_for('main.listar_clientes')) 

    cliente_obj = contato.cliente 
    cliente_public_id = cliente_obj.public_id if cliente_obj else None
    
    era_principal = contato.is_principal
    nome_contato_excluido = contato.nome

    try:
        db.session.delete(contato)
        if era_principal and cliente_obj:
            cliente_obj.email_principal = None
            cliente_obj.telefone_principal = None
        
        db.session.commit()
        flash(f'Contato "{nome_contato_excluido}" excluído com sucesso.', 'success')

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Erro ao excluir contato {contato_id}: {e}", exc_info=True)
        flash(f'Erro ao excluir contato: {str(e)}', 'danger')
    
    if cliente_public_id:
        return redirect(url_for('main.view_cliente', public_id=cliente_public_id))
    else:
        return redirect(url_for('main.listar_clientes'))


@main_bp.route('/boletos')
def listar_boletos():
    todos_os_boletos = Boleto.query.join(Cliente).order_by(Boleto.status.asc(), Boleto.data_vencimento.asc(), Boleto.grupo_replicacao_id, Boleto.parcela_atual.asc()).all()
    todos_clientes_para_modal = Cliente.query.order_by(Cliente.nome).all()
    return render_template('boletos.html',
                           lista_completa_boletos=todos_os_boletos,
                           todos_clientes_para_modal=todos_clientes_para_modal)

@main_bp.route('/boletos/novo', methods=['GET', 'POST'])
def novo_boleto():
    clientes = Cliente.query.order_by(Cliente.nome).all()
    cliente_id_param = request.args.get('cliente_id')
    cliente_public_id_param_redirect = request.args.get('redirect_to_client_public_id') 

    form_data_inicial = {'numero_parcelas': 1, 'periodicidade_replicacao': 'mensal'}
    if cliente_id_param: 
        form_data_inicial['cliente_id'] = cliente_id_param
    
    if not clientes:
        flash('Cadastre um cliente antes de adicionar um boleto.', 'warning')
        return redirect(url_for('main.novo_cliente'))

    if request.method == 'POST':
        cliente_id_interno = request.form.get('cliente_id')
        descricao_base = request.form.get('descricao_base', 'Cobrança')
        valor_str = request.form.get('valor')
        data_vencimento_str = request.form.get('data_vencimento')
        periodicidade = request.form.get('periodicidade_replicacao')
        numero_parcelas_str = request.form.get('numero_parcelas', '1')
        redirect_to_client_public_id_post = request.form.get('redirect_to_client_public_id')

        erros = []
        if not cliente_id_interno: erros.append("Cliente é obrigatório.")
        if not valor_str: erros.append("Valor é obrigatório.")
        if not data_vencimento_str: erros.append("Data de vencimento é obrigatória.")
        if not periodicidade: erros.append("Periodicidade é obrigatória.")
        if not numero_parcelas_str: erros.append("Número de parcelas é obrigatório.")
        
        valor = None; data_vencimento_inicial = None; numero_parcelas = 1
        try:
            if valor_str: valor = float(valor_str.replace('.', '').replace(',', '.'))
            if data_vencimento_str: data_vencimento_inicial = datetime.strptime(data_vencimento_str, '%Y-%m-%d').date()
            if numero_parcelas_str: numero_parcelas = int(numero_parcelas_str)
        except ValueError: erros.append("Formato de valor, data ou parcelas inválido.")

        if valor is not None and valor <= 0: erros.append("Valor deve ser positivo.")
        if numero_parcelas < 1: erros.append("Número de parcelas deve ser pelo menos 1.")
        if periodicidade == 'unico' and numero_parcelas > 1: numero_parcelas = 1
        
        if erros:
            for erro in erros: flash(erro, 'danger')
            return render_template('form_boleto.html', clientes=clientes, form_data=request.form, redirect_to_client_public_id=redirect_to_client_public_id_post or cliente_public_id_param_redirect)

        try:
            grupo_id = str(uuid.uuid4())
            cliente_obj = db.session.get(Cliente, int(cliente_id_interno))
            if not cliente_obj:
                flash(f'Cliente com ID {cliente_id_interno} não encontrado.', 'danger')
                return render_template('form_boleto.html', clientes=clientes, form_data=request.form, redirect_to_client_public_id=redirect_to_client_public_id_post or cliente_public_id_param_redirect)
            
            for i in range(numero_parcelas):
                data_venc_parcela_atual = data_vencimento_inicial
                if i > 0:
                    if periodicidade == 'semanal': data_venc_parcela_atual = data_vencimento_inicial + timedelta(weeks=i)
                    elif periodicidade == 'quinzenal': data_venc_parcela_atual = data_vencimento_inicial + timedelta(days=15*i)
                    elif periodicidade == 'mensal': data_venc_parcela_atual = data_vencimento_inicial + relativedelta(months=i)
                
                descricao_final = f"{descricao_base} ({i+1}/{numero_parcelas})" if numero_parcelas > 1 else descricao_base
                
                novo_boleto_obj = Boleto(
                    cliente_id=cliente_obj.id, descricao_base=descricao_base, descricao_completa=descricao_final,
                    valor=valor, data_vencimento=data_venc_parcela_atual, periodicidade_replicacao=periodicidade,
                    numero_parcelas=numero_parcelas, grupo_replicacao_id=grupo_id if numero_parcelas > 1 else None, 
                    parcela_atual=i+1, status='pendente'
                )
                db.session.add(novo_boleto_obj)
            db.session.commit()
            
            flash(f'{numero_parcelas if numero_parcelas > 1 else "Boleto"} para "{cliente_obj.nome}" cadastrado(s)!', 'success')
            
            final_redirect_id = redirect_to_client_public_id_post or cliente_public_id_param_redirect
            if final_redirect_id:
                return redirect(url_for('main.view_cliente', public_id=final_redirect_id))
            return redirect(url_for('main.listar_boletos'))
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao cadastrar boleto(s): {str(e)}', 'danger')
            return render_template('form_boleto.html', clientes=clientes, form_data=request.form, redirect_to_client_public_id=redirect_to_client_public_id_post or cliente_public_id_param_redirect)
    return render_template('form_boleto.html', clientes=clientes, form_data=form_data_inicial, redirect_to_client_public_id=cliente_public_id_param_redirect)

@main_bp.route('/boletos/<string:public_id>/form_modal_content')
def form_boleto_modal_content(public_id):
    boleto = Boleto.query.filter_by(public_id=public_id).first_or_404()
    clientes_dropdown = Cliente.query.order_by(Cliente.nome).all()
    form_data = {
        'public_id': boleto.public_id, 'cliente_id': boleto.cliente_id, 'descricao_base': boleto.descricao_base,
        'valor': str(boleto.valor).replace('.', ',') if boleto.valor is not None else '',
        'data_vencimento': boleto.data_vencimento.strftime('%Y-%m-%d') if boleto.data_vencimento else '',
        'periodicidade_replicacao': boleto.periodicidade_replicacao, 'numero_parcelas': boleto.numero_parcelas, 'status': boleto.status
    }
    return render_template('modal_boleto.html', boleto=boleto, clientes_dropdown=clientes_dropdown, form_data=form_data, edit_mode=True, readonly_parcelas=(boleto.numero_parcelas > 1 and boleto.status != 'pago'), public_id=boleto.public_id)

@main_bp.route('/boletos/<string:public_id>/editar_ajax', methods=['POST'])
def editar_boleto_ajax(public_id):
    boleto = Boleto.query.filter_by(public_id=public_id).first_or_404()
    erros_msg = []
    is_pago_str = request.form.get('is_pago', 'false')
    is_pago_submetido = is_pago_str.lower() == 'true'
    novo_status_desejado = 'pago' if is_pago_submetido else 'pendente'
    permitir_edicao_campos = True

    if novo_status_desejado == 'pago':
        boleto.status = 'pago'
        if not boleto.data_pagamento: boleto.data_pagamento = date.today()
        permitir_edicao_campos = False
    else:
        boleto.status = 'pendente'
        boleto.data_pagamento = None
        permitir_edicao_campos = True

    if permitir_edicao_campos:
        if boleto.grupo_replicacao_id and boleto.numero_parcelas > 1:
            num_parcelas_form = request.form.get('numero_parcelas')
            periodicidade_form = request.form.get('periodicidade_replicacao')
            num_parcelas_form_int = int(num_parcelas_form) if num_parcelas_form is not None else None
            if num_parcelas_form_int != boleto.numero_parcelas or periodicidade_form != boleto.periodicidade_replicacao:
                db.session.rollback()
                return jsonify(success=False, message='Não é possível alterar parcelas/periodicidade de um boleto que faz parte de uma série.')
        try:
            submitted_cliente_id = request.form.get('cliente_id')
            if submitted_cliente_id: boleto.cliente_id = int(submitted_cliente_id)
            else:
                hidden_cliente_id = request.form.get('cliente_id_hidden_se_pago_modal')
                if hidden_cliente_id: boleto.cliente_id = int(hidden_cliente_id)
                else: erros_msg.append('Cliente é obrigatório.')

            boleto.descricao_base = request.form.get('descricao_base', boleto.descricao_base)
            if not boleto.descricao_base.strip(): erros_msg.append('Descrição base não pode ser vazia.')
            valor_str = request.form.get('valor')
            if valor_str:
                valor_formatado = float(valor_str.replace('.', '').replace(',', '.'))
                if valor_formatado <= 0: erros_msg.append("Valor deve ser positivo.")
                else: boleto.valor = valor_formatado
            else: erros_msg.append("Valor é obrigatório.")
            data_vencimento_str = request.form.get('data_vencimento')
            if data_vencimento_str: boleto.data_vencimento = datetime.strptime(data_vencimento_str, '%Y-%m-%d').date()
            else: erros_msg.append("Data de vencimento é obrigatória.")

            if not (boleto.grupo_replicacao_id and boleto.numero_parcelas > 1) :
                boleto.periodicidade_replicacao = request.form.get('periodicidade_replicacao', boleto.periodicidade_replicacao)
                num_parcelas_str_edit = request.form.get('numero_parcelas')
                if num_parcelas_str_edit:
                    try:
                        num_parcelas_edit = int(num_parcelas_str_edit)
                        if num_parcelas_edit >=1:
                            boleto.numero_parcelas = num_parcelas_edit
                            if boleto.periodicidade_replicacao == 'unico': boleto.numero_parcelas = 1
                        else: erros_msg.append("Número de parcelas deve ser ao menos 1.")
                    except ValueError: erros_msg.append("Número de parcelas inválido.")
                else: erros_msg.append("Número de parcelas é obrigatório.")
            
            if boleto.numero_parcelas > 1: boleto.descricao_completa = f"{boleto.descricao_base} ({boleto.parcela_atual}/{boleto.numero_parcelas})"
            else:
                boleto.descricao_completa = boleto.descricao_base
                boleto.grupo_replicacao_id = None
        except ValueError as ve: erros_msg.append(f"Erro de valor: {ve}")
        except Exception as ex: erros_msg.append(f"Erro inesperado: {ex}")

    if erros_msg:
        db.session.rollback()
        return jsonify(success=False, message="; ".join(erros_msg))
    try:
        db.session.commit()
        return jsonify(success=True, message='Boleto atualizado!')
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f'Erro ao atualizar: {str(e)}')

@main_bp.route('/boletos/<string:public_id>/marcar_status_ajax', methods=['POST'])
def marcar_status_boleto_ajax(public_id):
    boleto = Boleto.query.filter_by(public_id=public_id).first_or_404()
    novo_status = request.form.get('novo_status')
    mensagem = ""
    if novo_status == 'pago':
        boleto.status = 'pago'
        if not boleto.data_pagamento: boleto.data_pagamento = date.today()
        mensagem = f'Boleto {boleto.descricao_completa or boleto.descricao_base} marcado como PAGO.'
    elif novo_status == 'pendente':
        boleto.status = 'pendente'
        boleto.data_pagamento = None
        mensagem = f'Boleto {boleto.descricao_completa or boleto.descricao_base} marcado como PENDENTE.'
    else: return jsonify(success=False, message='Status inválido.')
    try:
        db.session.commit()
        return jsonify(success=True, message=mensagem, boleto={'status': boleto.status, 'data_pagamento_formatada': boleto.data_pagamento.strftime('%d/%m/%Y') if boleto.data_pagamento else None})
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f'Erro ao alterar status: {str(e)}')

@main_bp.route('/boletos/<string:public_id>/excluir', methods=['POST'])
def excluir_boleto(public_id):
    boleto = Boleto.query.filter_by(public_id=public_id).first_or_404()
    cliente_public_id_para_redirect = boleto.cliente.public_id if boleto.cliente else None
    nome_boleto_desc = boleto.descricao_completa or boleto.descricao_base
    try:
        db.session.delete(boleto)
        db.session.commit()
        flash(f'Boleto "{nome_boleto_desc}" excluído.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao excluir boleto: {str(e)}', 'danger')
    if request.form.get('origin') == 'view_cliente' and cliente_public_id_para_redirect:
         return redirect(url_for('main.view_cliente', public_id=cliente_public_id_para_redirect))
    return redirect(url_for('main.listar_boletos'))

@main_bp.route('/boletos/grupo/<string:grupo_id>/excluir', methods=['POST'])
def excluir_grupo_boletos(grupo_id):
    boletos_do_grupo = Boleto.query.filter_by(grupo_replicacao_id=grupo_id).all()
    if not boletos_do_grupo:
        flash('Série de boletos não encontrada.', 'warning')
        return redirect(request.referrer or url_for('main.listar_boletos'))
    cliente_public_id_para_redirect = boletos_do_grupo[0].cliente.public_id if boletos_do_grupo[0].cliente else None
    desc_serie = boletos_do_grupo[0].descricao_base
    num_parcelas_serie = boletos_do_grupo[0].numero_parcelas
    try:
        for b_item in boletos_do_grupo: db.session.delete(b_item)
        db.session.commit()
        flash(f'Série "{desc_serie}" ({num_parcelas_serie}p) excluída.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao excluir série: {str(e)}', 'danger')
    if request.form.get('origin') == 'view_cliente' and cliente_public_id_para_redirect:
         return redirect(url_for('main.view_cliente', public_id=cliente_public_id_para_redirect))
    return redirect(url_for('main.listar_boletos'))

@main_bp.route('/boletos/<string:public_id>/dados_json', methods=['GET'])
def dados_boleto_json(public_id):
    boleto = Boleto.query.filter_by(public_id=public_id).first_or_404()
    clientes_dropdown = [{'id': cli.id, 'nome': cli.nome, 'email': cli.email_principal or ''} for cli in Cliente.query.order_by(Cliente.nome).all()]
    boleto_data = {
        'public_id': boleto.public_id, 'cliente_id': boleto.cliente_id, 'descricao_base': boleto.descricao_base,
        'valor': f"{boleto.valor:,.2f}".replace(',', '#').replace('.', ',').replace('#', '.') if boleto.valor is not None else '',
        'data_vencimento': boleto.data_vencimento.strftime('%Y-%m-%d') if boleto.data_vencimento else '',
        'periodicidade_replicacao': boleto.periodicidade_replicacao, 'numero_parcelas': boleto.numero_parcelas,
        'status': boleto.status, 'data_pagamento': boleto.data_vencimento.strftime('%d/%m/%Y') if boleto.data_pagamento else None,
        'readonly_parcelas': (boleto.grupo_replicacao_id is not None and boleto.numero_parcelas > 1 and boleto.status != 'pago')
    }
    return jsonify(boleto=boleto_data, clientes_dropdown=clientes_dropdown)

# --- Rota de Configurações de E-mail (Simplificada para Gmail) ---
@main_bp.route('/configuracoes/email', methods=['GET', 'POST'])
def configuracoes_email():
    config = ConfiguracaoEmail.query.first()
    form_data_display = {}
    senha_configurada_anteriormente = bool(config and config.senha_remetente)

    if request.method == 'POST':
        email_remetente_gmail = request.form.get('email_remetente_gmail')
        senha_remetente_gmail = request.form.get('senha_remetente_gmail') 
        nome_remetente = request.form.get('nome_remetente', 'Sistema de Cobranças')
        texto_padrao_email = request.form.get('texto_padrao_email') 
        dias_antecedencia_str = request.form.get('dias_antecedencia_vencimento')
        notificar_atrasados = 'notificar_atrasados' in request.form # Capturar o valor do toggle/checkbox
        app.logger.debug(f"Valor de 'notificar_atrasados' recebido do formulário: {request.form.get('notificar_atrasados')}")
        app.logger.debug(f"Valor booleano processado para 'notificar_atrasados': {notificar_atrasados}")
        dia_semana_envio = request.form.get('dia_semana_envio')
        horario_envio_str = request.form.get('horario_envio')
        chave_api_gemini = request.form.get('chave_api_gemini')

        if not senha_remetente_gmail and senha_configurada_anteriormente: # Mantém a senha se não for submetida
            senha_para_salvar = config.senha_remetente
        else:
            senha_para_salvar = senha_remetente_gmail

        if not email_remetente_gmail or not senha_para_salvar:
            flash('E-mail Gmail e Senha de App são obrigatórios.', 'warning')
            form_data_repopulate = request.form.to_dict()
            if config: 
                form_data_repopulate['nome_remetente'] = config.nome_remetente if 'nome_remetente' not in form_data_repopulate else form_data_repopulate['nome_remetente']
                form_data_repopulate['texto_padrao_email'] = config.texto_padrao_email if 'texto_padrao_email' not in form_data_repopulate else form_data_repopulate['texto_padrao_email']
                if config.horario_envio and 'horario_envio' not in form_data_repopulate :
                     form_data_repopulate['horario_envio'] = config.horario_envio.strftime('%H:%M')
                form_data_repopulate['dias_antecedencia_vencimento'] = config.dias_antecedencia_vencimento if 'dias_antecedencia_vencimento' not in form_data_repopulate else form_data_repopulate['dias_antecedencia_vencimento']
                form_data_repopulate['notificar_atrasados'] = request.form.get('notificar_atrasados') == 'on'
                form_data_repopulate['chave_api_gemini'] = 'Configurada' if config.chave_api_gemini else '' # Não repopula a chave
                form_data_repopulate['dia_semana_envio'] = config.dia_semana_envio if 'dia_semana_envio' not in form_data_repopulate else form_data_repopulate['dia_semana_envio']
            form_data_repopulate['senha_configurada'] = senha_configurada_anteriormente
            return render_template('configuracoes.html', config=config, form_data=form_data_repopulate)

        horario_envio_obj = None
        if horario_envio_str:
            try: horario_envio_obj = datetime.strptime(horario_envio_str, '%H:%M').time()
            except ValueError:
                flash('Formato de horário inválido. Use HH:MM.', 'warning')
                form_data_repopulate = request.form.to_dict()
                form_data_repopulate['senha_configurada'] = senha_configurada_anteriormente
                return render_template('configuracoes.html', config=config, form_data=form_data_repopulate)
        
        dias_antecedencia_obj = 0 
        if dias_antecedencia_str:
            try:
                dias_antecedencia_obj = int(dias_antecedencia_str)
                if dias_antecedencia_obj < 0:
                    flash('Dias de antecedência não podem ser negativos.', 'warning')
                    form_data_repopulate = request.form.to_dict()
                    form_data_repopulate['senha_configurada'] = senha_configurada_anteriormente
                    return render_template('configuracoes.html', config=config, form_data=form_data_repopulate)
            except ValueError:
                flash('Dias de antecedência devem ser um número.', 'warning')
                form_data_repopulate = request.form.to_dict()
                form_data_repopulate['senha_configurada'] = senha_configurada_anteriormente
                return render_template('configuracoes.html', config=config, form_data=form_data_repopulate)
        
        try:
            if not config:
                config = ConfiguracaoEmail()
                db.session.add(config)
                app.logger.debug(f"Salvando 'notificar_atrasados' como: {notificar_atrasados}")
            
            config.email_remetente = email_remetente_gmail
            config.senha_remetente = senha_para_salvar 
            config.nome_remetente = nome_remetente
            config.texto_padrao_email = texto_padrao_email
            config.dias_antecedencia_vencimento = dias_antecedencia_obj
            config.notificar_atrasados = notificar_atrasados
            config.chave_api_gemini = chave_api_gemini if chave_api_gemini else (config.chave_api_gemini if config and not chave_api_gemini else None) # Mantém a chave se vazio, a menos que seja a primeira configuração
            config.dia_semana_envio = dia_semana_envio
            config.horario_envio = horario_envio_obj
            
            config.mail_server = "smtp.gmail.com" 
            config.mail_port = 587                
            config.mail_use_tls = True            
            config.mail_use_ssl = False           
            
            db.session.commit()
            flash('Configurações de e-mail (Gmail) salvas com sucesso!', 'success')
            senha_configurada_anteriormente = bool(config.senha_remetente)

            if scheduler.running: 
                try: scheduler.remove_job('job_enviar_notificacoes_agendadas_id')
                except JobLookupError: pass 
                if config.horario_envio and config.email_remetente: 
                    scheduler.add_job(func=tarefa_enviar_notificacoes_agendadas, trigger=CronTrigger(hour=config.horario_envio.hour, minute=config.horario_envio.minute, timezone=scheduler.timezone), id='job_enviar_notificacoes_agendadas_id', name='Enviar notificações agendadas', replace_existing=True)
                    app.logger.info(f"APScheduler: Job reagendado para {config.horario_envio.strftime('%H:%M')}.")
                else: app.logger.info("APScheduler: Job não reagendado (configs de horário ou e-mail Gmail incompletas).")
            return redirect(url_for('main.configuracoes_email'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Erro ao salvar configurações de e-mail: {e}", exc_info=True)
            flash(f'Erro ao salvar configurações: {str(e)}', 'danger')
            form_data_repopulate = request.form.to_dict()
            form_data_repopulate['senha_configurada'] = senha_configurada_anteriormente
            return render_template('configuracoes.html', config=config, form_data=form_data_repopulate)

    if config:
        form_data_display = {
            'email_remetente_gmail': config.email_remetente,
            'nome_remetente': config.nome_remetente,
            'texto_padrao_email': config.texto_padrao_email,
            'dias_antecedencia_vencimento': config.dias_antecedencia_vencimento,
            'notificar_atrasados': config.notificar_atrasados,
            'chave_api_gemini': 'Configurada' if config.chave_api_gemini else '', # Passa placeholder
            'dia_semana_envio': config.dia_semana_envio,
            'horario_envio': config.horario_envio.strftime('%H:%M') if config.horario_envio else '09:00',
            'senha_configurada': senha_configurada_anteriormente
        }
    else: 
        form_data_display = {
            'email_remetente_gmail': '',
            'nome_remetente': 'Sistema de Cobranças',
            'texto_padrao_email': "<p>Olá {nome_contato},</p><p>Segue o boleto referente a <strong>{descricao_boleto}</strong> no valor de <strong>R$ {valor_boleto}</strong>, com vencimento em <em>{data_vencimento}</em>.</p><p>Lembramos que também constam os seguintes vencimentos em aberto: {lista_datas_vencidas}.</p><p>Qualquer dúvida, estamos à disposição.</p><p>Atenciosamente,<br>{nome_remetente_empresa}</p>",
            'dias_antecedencia_vencimento': 3, 
            'notificar_atrasados': True,
            'chave_api_gemini': '', # Placeholder
            'dia_semana_envio': 'segunda', 
            'horario_envio': '09:00',
            'senha_configurada': False 
        }
    return render_template('configuracoes.html', config=config, form_data=form_data_display)

@main_bp.route('/configuracoes/email/processar_manualmente', methods=['GET'])
def processar_notificacoes_manualmente():
    config = ConfiguracaoEmail.query.first()
    if not config or not all([config.email_remetente, config.senha_remetente, 
                               config.texto_padrao_email, 
                               config.dias_antecedencia_vencimento is not None]):
        flash('Configurações de E-mail Gmail (usuário, senha de app), template ou agendamento incompletas.', 'warning')
        return redirect(url_for('main.configuracoes_email'))

    hoje = date.today()
    # Para a notificação manual, consideramos os boletos que se encaixam no critério de antecedência
    # ou que já venceram (dias_antecedencia_vencimento pode ser 0 para "no dia", ou negativo se quisesse notificar após vencido)
    data_limite_superior = hoje + timedelta(days=config.dias_antecedencia_vencimento)
    
    boletos_para_notificar = Boleto.query.filter(
        Boleto.status == 'pendente', 
        Boleto.data_vencimento <= data_limite_superior, # Inclui boletos vencendo até X dias no futuro
        Boleto.data_vencimento >= hoje # Notifica boletos que vencem hoje ou nos próximos X dias
                                      # Para incluir vencidos, a lógica do filtro ou da tag {lista_datas_vencidas} já cuida disso
    ).all()
    
    enviados_sucesso, enviados_falha = 0, 0
    if not boletos_para_notificar:
        flash('Nenhum boleto encontrado para notificação manual com os critérios atuais.', 'info')
        return redirect(url_for('main.configuracoes_email'))

    for boleto_item in boletos_para_notificar:
        if enviar_email_cobranca(boleto_item.id): enviados_sucesso += 1
        else: enviados_falha += 1
            
    flash_msg = f"Processamento manual: {enviados_sucesso} enviadas."
    if enviados_falha > 0: flash_msg += f" {enviados_falha} falharam."; flash(flash_msg, 'warning')
    else: flash(flash_msg, 'success')
    return redirect(url_for('main.configuracoes_email'))

# --- Rota para Visualização de Logs de E-mail ---
@main_bp.route('/logs_email')
def listar_logs_email():
    log_entries = db.session.query(EmailLog).order_by(EmailLog.horario_disparo.desc()).all()
    print(log_entries)
    return render_template('logs_email.html', logs=log_entries)

@main_bp.route('/logs_email/<int:log_id>/dados_json')
def dados_log_email_json(log_id):
    log = db.session.get(EmailLog, log_id)
    if not log:
       return jsonify({"error": "Log não encontrado."}), 404

        # Preparar os dados para JSON, tratando None e objetos relacionados
    cliente_nome = log.cliente.nome if log.cliente else 'Cliente Removido'
    boleto_public_id = log.boleto.public_id if log.boleto else 'Boleto Removido'
    boleto_descricao = (log.boleto.descricao_completa or log.boleto.descricao_base) if log.boleto else 'Boleto Removido'
    boleto_data_vencimento = log.boleto.data_vencimento.strftime('%d/%m/%Y') if log.boleto and log.boleto.data_vencimento else 'N/D'
    
    log_data = {
    'id': log.id,
    'horario_disparo': log.horario_disparo.strftime('%d/%m/%Y %H:%M:%S') if log.horario_disparo else 'N/D',
    'email_remetente': log.email_remetente or 'N/D',
    'email_destinatario': log.email_destinatario or 'N/D',
    'email_cc': log.email_cc or 'N/D',
    'assunto': log.assunto or 'Sem Assunto',
    'mensagem_corpo': log.mensagem_corpo or 'Corpo da mensagem vazio.',
    'status': log.status or 'desconhecido',
    'cliente_id': log.cliente_id,
    'cliente_nome': cliente_nome,
    'boleto_id': log.boleto_id,
 'boleto_descricao': boleto_descricao,
 'boleto_data_vencimento': boleto_data_vencimento,
    'boleto_public_id': boleto_public_id,
    'detalhes': log.detalhes or 'Sem detalhes adicionais.'
    }

    return jsonify(log_data)

@main_bp.route('/logs_email/<int:log_id>')
def visualizar_log_email(log_id):
    log = db.session.get(EmailLog, log_id)
    return render_template('modal_log_email_detalhes.html', log=log)
# --- Função de Envio de E-mail (com CID embedding e tag {nome_contato} e {lista_datas_vencidas}) ---
def enviar_email_cobranca(boleto_id_interno):
    with app.app_context():
        boleto = db.session.get(Boleto, boleto_id_interno)
        if not boleto:
            app.logger.error(f"Boleto não encontrado: ID Interno {boleto_id_interno}")
            return False

        cliente = boleto.cliente
        if not cliente:
            app.logger.error(f"Boleto ID Interno {boleto_id_interno} (PubID: {boleto.public_id}) não tem cliente associado.")
            return False

        config_email = ConfiguracaoEmail.query.first()
        if not config_email or not all([
            config_email.email_remetente,
            config_email.senha_remetente,
            config_email.texto_padrao_email
        ]):
            app.logger.error(f"Configurações de E-mail Gmail (usuário, senha de app) ou template incompletas. E-mail não enviado para boleto {boleto.public_id}.")
            return False

        destinatario_para = None
        nome_contato_para_template = cliente.nome
        contato_principal = Contato.query.filter_by(cliente_id=cliente.id, is_principal=True).first()

        if contato_principal:
            if contato_principal.email:
                destinatario_para = contato_principal.email
            if contato_principal.nome:
                nome_contato_para_template = contato_principal.nome
        elif cliente.email_principal:
            destinatario_para = cliente.email_principal

        if not destinatario_para:
            app.logger.warning(f"Não foi possível determinar destinatário para cliente {cliente.nome}. E-mail não enviado para boleto {boleto.public_id}.")
            return False

        destinatarios_cc_lista = [c.email for c in cliente.contatos if c.email and c.email != destinatario_para and not (c.is_principal and c.email == destinatario_para)]
        destinatarios_cc_str = ", ".join(list(set(destinatarios_cc_lista)))

        # Buscar boletos vencidos para a tag {lista_datas_vencidas}
        hoje_para_vencidos = date.today() # Usar data atual para definir "vencido"
        boletos_vencidos_cliente = Boleto.query.filter(
            Boleto.cliente_id == cliente.id,
            Boleto.status == 'pendente',
            Boleto.data_vencimento < hoje_para_vencidos
        ).order_by(Boleto.data_vencimento.asc()).all()

        lista_datas_vencidas_str = "Nenhuma"
        if boletos_vencidos_cliente:
            datas_formatadas = [b_venc.data_vencimento.strftime('%d/%m/%Y') for b_venc in boletos_vencidos_cliente]
            lista_datas_vencidas_str = ", ".join(datas_formatadas)

        app.logger.debug(f"Cliente {cliente.nome}, Boletos vencidos: {lista_datas_vencidas_str}")

        # Aqui você pode usar config_email.chave_api_gemini e config_email.notificar_atrasados
        # para ajustar o texto do e-mail, se necessário. Ex: se notificar_atrasados=True
        # e chave_api_gemini existe, usar a API para gerar um texto diferente para a lista.
        # A implementação da chamada da API em si não está neste diff.

        corpo_email_html_template = config_email.texto_padrao_email.format(
            nombre_contato=nome_contato_para_template,
            id_boleto=boleto.public_id,
            descricao_boleto=boleto.descricao_completa or boleto.descricao_base or "Serviços Prestados",
            nome_remetente_empresa=config_email.nome_remetente or "Sua Empresa"
        )
        assunto_email = f"Cobrança: {boleto.descricao_completa or boleto.descricao_base} - Venc: {boleto.data_vencimento.strftime('%d/%m/%Y') if boleto.data_vencimento else 'N/D'}"
        if config_email.nome_remetente:
            remetente_formatado = formataddr((config_email.nome_remetente, config_email.email_remetente))
        else:
            remetente_formatado = config_email.email_remetente
            
        format_params = {
            'data_vencimento': boleto.data_vencimento.strftime('%d/%m/%Y') if boleto.data_vencimento else "N/D",
            'valor_boleto': f"{boleto.valor:,.2f}".replace(',', '#').replace('.', ',').replace('#', '.') if boleto.valor is not None else "N/D",
            'lista_datas_vencidas': lista_datas_vencidas_str if config_email.notificar_atrasados else "Nenhuma" # Só inclui a lista se a notificação de atrasados estiver ligada
        }

        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = assunto_email
        msg_root['From'] = remetente_formatado
        msg_root['To'] = destinatario_para
        if destinatarios_cc_str:
            msg_root['Cc'] = destinatarios_cc_str

        corpo_email_processado = corpo_email_html_template.format(**format_params)
        img_pattern = re.compile(r'<img[^>]*src="data:image/(png|jpeg|gif|webp);base64,([^"]+)"[^>]*>')
        image_parts = [] 

        matches = list(img_pattern.finditer(corpo_email_html_template))
        for i, match in enumerate(matches):
            img_type = match.group(1)
            base64_data = match.group(2)
            try:
                image_data = base64.b64decode(base64_data)
                image_cid = f'image{i+1}_{uuid.uuid4().hex}'
                img_part = MIMEImage(image_data, _subtype=img_type)
                img_part.add_header('Content-ID', f'<{image_cid}>')
                img_part.add_header('Content-Disposition', 'inline', filename=f'{image_cid}.{img_type}')
                image_parts.append(img_part)
                corpo_email_processado = corpo_email_processado.replace(f"data:image/{img_type};base64,{base64_data}", f"cid:{image_cid}", 1)
                app.logger.debug(f"Imagem base64 embutida com CID: {image_cid}")
            except Exception as e_img:
                app.logger.error(f"Erro ao processar imagem base64: {e_img}")
                pass

        msg_alternative = MIMEMultipart('alternative')
        msg_alternative.attach(MIMEText(corpo_email_processado, 'html', 'utf-8'))
        msg_root.attach(msg_alternative)

        for img_part in image_parts:
            msg_root.attach(img_part)

        gmail_server = "smtp.gmail.com"
        gmail_port = 587

        server = None
        try:
            app.logger.debug(f"Conectando via SMTP a {gmail_server}:{gmail_port}")
            server = smtplib.SMTP(gmail_server, gmail_port, timeout=20)
            app.logger.debug("Iniciando TLS...")
            server.starttls()

            app.logger.debug(f"Fazendo login com usuário Gmail: {config_email.email_remetente}")
            app.logger.info(f"CREDENCIAIS PARA LOGIN SMTP: Usuário='{config_email.email_remetente}', Senha (primeiros/últimos 4 chars)='{config_email.senha_remetente[:4]}...{config_email.senha_remetente[-4:] if config_email.senha_remetente and len(config_email.senha_remetente) > 7 else 'N/A'}'")
            server.login(config_email.email_remetente, config_email.senha_remetente)

            todos_os_destinatarios = [destinatario_para] + list(set(destinatarios_cc_lista))

            app.logger.debug(f"Enviando mensagem para: {todos_os_destinatarios}")
            server.send_message(msg_root)

            app.logger.info(f"E-mail para boleto {boleto.public_id} enviado para {destinatario_para} (Cc: {destinatarios_cc_str}). Servidor: {gmail_server}.")

            # REGISTRAR LOG DE SUCESSO
            novo_log = EmailLog(
 horario_disparo=datetime.now(),
                email_remetente=config_email.email_remetente,
                email_destinatario=destinatario_para,
 # Incluir email_cc SOMENTE se houver destinatarios_cc_str
 **({'email_cc': destinatarios_cc_str} if destinatarios_cc_str else {}),
                assunto=assunto_email,
                mensagem_corpo=corpo_email_processado,
                status='sucesso',
                cliente_id=cliente.id,
                boleto_id=boleto.id,
 data_boleto=boleto.data_vencimento, # <-- Adicionado aqui
                detalhes="E-mail enviado com sucesso."
            )
            db.session.add(novo_log)
            db.session.commit() # Commit do log
            app.logger.info(f"Log de e-mail de sucesso registrado para boleto {boleto.public_id}.")

            return True
        except smtplib.SMTPAuthenticationError as e_auth:
            app.logger.error(f"Falha de autenticação ao enviar e-mail para boleto {boleto.public_id} via Gmail: {e_auth}", exc_info=True)
            # REGISTRAR LOG DE FALHA (AUTENTICAÇÃO)
            novo_log = EmailLog(
                horario_disparo=datetime.now(),
                email_remetente=config_email.email_remetente,
                email_destinatario=destinatario_para,
                # Incluir email_cc SOMENTE se houver destinatarios_cc_str
 **({'email_cc': destinatarios_cc_str} if destinatarios_cc_str else {}),
                assunto=assunto_email,
                mensagem_corpo=corpo_email_processado, # Pode ser útil para debug

                status='falha_autenticacao',
                cliente_id=cliente.id,
                boleto_id=boleto.id,
 data_boleto=boleto.data_vencimento, # <-- Adicionado aqui
                detalhes=f"Falha de autenticação: {e_auth}"
            )
            db.session.add(novo_log)
            db.session.commit() # Commit do log
            return False
        except smtplib.SMTPServerDisconnected as e_dc:
            app.logger.error(f"Servidor Gmail desconectado ao enviar e-mail para boleto {boleto.public_id}: {e_dc}", exc_info=True)
            # REGISTRAR LOG DE FALHA (DESCONEXÃO)
            novo_log = EmailLog(
                horario_disparo=datetime.now(),
                email_remetente=config_email.email_remetente,
                email_destinatario=destinatario_para,
                # Incluir email_cc SOMENTE se houver destinatarios_cc_str
 **({'email_cc': destinatarios_cc_str} if destinatarios_cc_str else {}),
                assunto=assunto_email,
                mensagem_corpo=corpo_email_processado,

                status='falha_conexao',
                cliente_id=cliente.id,
                boleto_id=boleto.id,
 data_boleto=boleto.data_vencimento, # <-- Adicionado aqui
                detalhes=f"Servidor desconectado: {e_dc}"
            )
            db.session.add(novo_log)
            db.session.commit() # Commit do log
            return False
        except smtplib.SMTPException as e_smtp:
            app.logger.error(f"Erro SMTP ao enviar e-mail para boleto {boleto.public_id} via Gmail: {e_smtp}", exc_info=True)
            # REGISTRAR LOG DE FALHA (SMTP GENÉRICO)
            novo_log = EmailLog(
                horario_disparo=datetime.now(),
                email_remetente=config_email.email_remetente,
                email_destinatario=destinatario_para,
                # Incluir email_cc SOMENTE se houver destinatarios_cc_str
 **({'email_cc': destinatarios_cc_str} if destinatarios_cc_str else {}),
                assunto=assunto_email,
                mensagem_corpo=corpo_email_processado,

                status='falha_smtp',
                cliente_id=cliente.id,
                boleto_id=boleto.id,
 data_boleto=boleto.data_vencimento, # <-- Adicionado aqui
                detalhes=f"Erro SMTP: {e_smtp}"
            )
            db.session.add(novo_log)
            db.session.commit() # Commit do log
            return False
        except socket.gaierror as e_gaierror:
             app.logger.error(f"Erro de resolução de endereço (getaddrinfo failed) para {gmail_server}: {e_gaierror}", exc_info=True)
             # REGISTRAR LOG DE FALHA (RESOLUÇÃO DE ENDEREÇO)
             novo_log = EmailLog(
                horario_disparo=datetime.now(),
                email_remetente=config_email.email_remetente,
                email_destinatario=destinatario_para,
                # Incluir email_cc SOMENTE se houver destinatarios_cc_str
 **({'email_cc': destinatarios_cc_str} if destinatarios_cc_str else {}),
                assunto=assunto_email,
                mensagem_corpo=corpo_email_processado,

                status='falha_dns',
                cliente_id=cliente.id,
                boleto_id=boleto.id,
 data_boleto=boleto.data_vencimento, # <-- Adicionado aqui
                detalhes=f"Erro de resolução de endereço: {e_gaierror}"
            )
             db.session.add(novo_log)
             db.session.commit() # Commit do log
             return False
        except Exception as e:
            app.logger.error(f"Falha geral ao enviar e-mail para boleto {boleto.public_id} via Gmail: {e}", exc_info=True)
            # REGISTRAR LOG DE FALHA (ERRO GERAL)
            novo_log = EmailLog(
                horario_disparo=datetime.now(),
                email_remetente=config_email.email_remetente,
                email_destinatario=destinatario_para,
                # Incluir email_cc SOMENTE se houver destinatarios_cc_str
 **({'email_cc': destinatarios_cc_str} if destinatarios_cc_str else {}),
                assunto=assunto_email,
                mensagem_corpo=corpo_email_processado,

                status='falha_geral',
                cliente_id=cliente.id,
                boleto_id=boleto.id,
 data_boleto=boleto.data_vencimento, # <-- Adicionado aqui
                detalhes=f"Erro geral: {e}"
            )
            db.session.add(novo_log)
            db.session.commit() # Commit do log
            return False
        finally:
            if server:
                try:
                    server.quit()
                    app.logger.debug("Conexão SMTP com Gmail fechada.")
                except:
                    pass


# --- Tarefa Agendada ---
def tarefa_enviar_notificacoes_agendadas():
    with app.app_context(): 
        agora = datetime.now(scheduler.timezone) # Definir 'agora' no início para consistência
        app.logger.info(f"APScheduler: Iniciando tarefa_enviar_notificacoes_agendadas às {agora.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        
        config = ConfiguracaoEmail.query.first()
        if not config or not all([config.horario_envio, config.dia_semana_envio, 
                                   config.dias_antecedencia_vencimento is not None,
                                   config.email_remetente, config.senha_remetente]):
            app.logger.warning("APScheduler: Configs de agendamento ou E-mail Gmail incompletas. Envio não executado.")
            return
        
        app.logger.debug(f"APScheduler: Configs carregadas: Dia Envio={config.dia_semana_envio}, Horario Envio={config.horario_envio}, Antecedencia={config.dias_antecedencia_vencimento}")

        dia_semana_map = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}
        hoje_dia_num = agora.weekday() 
        enviar_hoje = False
        if config.dia_semana_envio == "todos": enviar_hoje = True
        elif config.dia_semana_envio == "uteis":
            if 0 <= hoje_dia_num <= 4: enviar_hoje = True
        elif config.dia_semana_envio in dia_semana_map:
            if dia_semana_map[config.dia_semana_envio] == hoje_dia_num: enviar_hoje = True
        
        if not enviar_hoje:
            app.logger.info(f"APScheduler: Hoje ({agora.strftime('%A')}) não é um dia configurado para envio ({config.dia_semana_envio}). Tarefa encerrada.")
            return
        
        # A verificação do horário exato é feita pelo CronTrigger do APScheduler.
        # A tarefa só é executada se o horário configurado for atingido.
        app.logger.info(f"APScheduler: Dia corresponde. Verificando boletos para {agora.strftime('%A')}.")
        
        dias_antecedencia = config.dias_antecedencia_vencimento
        data_atual_scheduler = agora.date() # Data atual baseada no fuso horário do scheduler
        
        data_limite_superior_notificacao = data_atual_scheduler + timedelta(days=dias_antecedencia)
        
        app.logger.debug(f"APScheduler: Critérios de data - Data Atual (Scheduler TZ): {data_atual_scheduler}, Limite Superior Notificação: {data_limite_superior_notificacao}")

        boletos_para_notificar = Boleto.query.filter(
            Boleto.status == 'pendente',
            Boleto.data_vencimento <= data_limite_superior_notificacao,
            Boleto.data_vencimento >= data_atual_scheduler 
        ).all()
        
        if not boletos_para_notificar:
            app.logger.info("APScheduler: Nenhum boleto encontrado para notificação agendada dentro dos critérios de data.")
            return
            
        app.logger.info(f"APScheduler: Encontrados {len(boletos_para_notificar)} boletos para notificação agendada.")
        enviados_sucesso, enviados_falha = 0, 0
        for boleto_item in boletos_para_notificar:
            app.logger.info(f"APScheduler: Processando boleto ID {boleto_item.public_id} (Venc: {boleto_item.data_vencimento})")
            if enviar_email_cobranca(boleto_item.id): 
                enviados_sucesso += 1
                app.logger.info(f"APScheduler: E-mail para boleto {boleto_item.public_id} enviado com sucesso pela tarefa agendada.")
            else: 
                enviados_falha += 1
                app.logger.warning(f"APScheduler: Falha ao enviar e-mail para boleto {boleto_item.public_id} pela tarefa agendada.")
        app.logger.info(f"APScheduler: Processamento da tarefa agendada concluído. {enviados_sucesso} e-mails enviados, {enviados_falha} falharam.")

# Registro do Blueprint e inicialização do Scheduler e App
app.register_blueprint(main_bp)

if not scheduler.running:
    try:
        scheduler.start()
        app.logger.info("APScheduler: Agendador iniciado.")
        atexit.register(lambda: scheduler.shutdown())
    except Exception as e:
        app.logger.error(f"APScheduler: Falha ao iniciar agendador: {e}", exc_info=True)

with app.app_context():
    db.create_all()

    email_config_inicial = ConfiguracaoEmail.query.first()
    if email_config_inicial and all([email_config_inicial.horario_envio,
                                     email_config_inicial.email_remetente,
                                     email_config_inicial.senha_remetente]):
        try:
            # Garante que o job não seja adicionado múltiplas vezes se já existir
            if scheduler.get_job('job_enviar_notificacoes_agendadas_id'):
                scheduler.remove_job('job_enviar_notificacoes_agendadas_id') # removedor
                app.logger.info("APScheduler: Job existente 'job_enviar_notificacoes_agendadas_id' removido para ser recriado.")
            scheduler.add_job(func=tarefa_enviar_notificacoes_agendadas,
                              trigger=CronTrigger(hour=email_config_inicial.horario_envio.hour,
                                                  minute=email_config_inicial.horario_envio.minute,
                                                  timezone=scheduler.timezone),
                              id='job_enviar_notificacoes_agendadas_id',
                              name='Enviar notificações agendadas de cobrança',
                              replace_existing=True) # replace_existing é importante
            app.logger.info(f"APScheduler: Job 'job_enviar_notificacoes_agendadas_id' agendado/reagendado na inicialização para as {email_config_inicial.horario_envio.strftime('%H:%M')}.")
        except Exception as e:
            app.logger.error(f"APScheduler: Erro ao agendar/reagendar job na inicialização: {e}", exc_info=True)
    else:
        app.logger.info("APScheduler: Horário de envio ou credenciais Gmail não definidas na tabela ConfiguracaoEmail. Job de notificações não agendado.")
if __name__ == '__main__':
    with app.app_context():
        db.drop_all()
        db.create_all()
        print("Banco de dados recriado!")
    app.run(debug=True, use_reloader=False)
