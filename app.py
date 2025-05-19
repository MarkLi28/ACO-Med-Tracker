import matplotlib
matplotlib.use('Agg') 
from flask import Flask, render_template, request, redirect, url_for, current_app
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, IntegerField, SelectField, SubmitField, SelectMultipleField, TextAreaField
from wtforms.validators import DataRequired
import matplotlib.pyplot as plt
import io
import base64
from collections import Counter
import json
from datetime import datetime
import threading
import time
import requests
from sqlalchemy.exc import SQLAlchemyError
from aws_cdk import (
    Stack,
    aws_apigateway as apigateway,
    aws_lambda as lambda_,
    aws_dynamodb as dynamodb,
    aws_cognito as cognito
)
from constructs import Construct
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///medical_aid.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['AWS_REGION'] = 'your-region'
app.config['AWS_ACCESS_KEY'] = 'your-access-key'
app.config['AWS_SECRET_KEY'] = 'your-secret-key'
app.config['COGNITO_USER_POOL_ID'] = 'your-user-pool-id'
app.config['COGNITO_APP_CLIENT_ID'] = 'your-app-client-id'

db = SQLAlchemy(app)

# Models
class Patient(db.Model):
    __tablename__ = 'patients'
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(10), nullable=False)
    blood_pressure = db.Column(db.String(20))

class Condition(db.Model):
    __tablename__ = 'conditions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

class Prescription(db.Model):
    __tablename__ = 'prescriptions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)


class PatientsConditions(db.Model):
    __tablename__ = 'patients_conditions'
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    condition_id = db.Column(db.Integer, db.ForeignKey('conditions.id'), nullable=False)

    condition = db.relationship('Condition')

class PatientsPrescriptions(db.Model):
    __tablename__ = 'patients_prescriptions'
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    prescription_id = db.Column(db.Integer, db.ForeignKey('prescriptions.id'), nullable=False)
    reason_for_diff_medication = db.Column(db.Text)

    prescription = db.relationship('Prescription')

class FolderSystem(db.Model):
    __tablename__ = 'folder_system'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    village = db.Column(db.String(80), nullable=False)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)

# Add new model for tracking sync status
class SyncQueue(db.Model):
    __tablename__ = 'sync_queue'
    id = db.Column(db.Integer, primary_key=True)
    data_type = db.Column(db.String(50), nullable=False)  # 'patient', 'condition', etc.
    data = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    synced = db.Column(db.Boolean, default=False)

# Add these models to your existing models
class Village(db.Model):
    __tablename__ = 'villages'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

class Year(db.Model):
    __tablename__ = 'years'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, unique=True, nullable=False)

# Forms
class TicketForm(FlaskForm):
    year = SelectField('Year', coerce=int, validators=[DataRequired()])
    village = SelectField('Village', validators=[DataRequired()])
    first_name = StringField('First Name', validators=[DataRequired()])
    last_name = StringField('Last Name', validators=[DataRequired()])
    age = IntegerField('Age', validators=[DataRequired()])
    gender = SelectField('Gender', choices=[('Male', 'Male'), ('Female', 'Female')], validators=[DataRequired()])
    blood_pressure = StringField('Blood Pressure')
    conditions = SelectMultipleField('Conditions', choices=[], coerce=int, validators=[DataRequired()])
    prescriptions = SelectMultipleField('Prescriptions', choices=[], coerce=int, validators=[DataRequired()])
    reason_for_diff_medication = TextAreaField('Reason for Different Medication')
    submit = SubmitField('Create Ticket')

class ConditionForm(FlaskForm):
    name = StringField('Condition Name', validators=[DataRequired()])
    submit = SubmitField('Add Condition')

class PrescriptionForm(FlaskForm):
    name = StringField('Prescription Name', validators=[DataRequired()])
    total_dose = StringField('Total Dose')
    daily_dose = StringField('Daily Dose')
    consumption_instructions = TextAreaField('Consumption Instructions')
    submit = SubmitField('Add Prescription')

# Add these form classes after your other form definitions
class VillageForm(FlaskForm):
    name = StringField('Village Name', validators=[DataRequired()])
    submit = SubmitField('Add Village')

class YearForm(FlaskForm):
    year = IntegerField('Year', validators=[DataRequired()])
    submit = SubmitField('Add Year')

# Helper Function to Generate Bar Charts
def generate_chart(data, x_label, y_label, title):
    # Sort the data alphabetically by keys (conditions/prescriptions)
    sorted_data = dict(sorted(data.items()))
    
    plt.figure(figsize=(5, 5))  # Make the figure wider
    bars = plt.bar(sorted_data.keys(), sorted_data.values())
    plt.xlabel(x_label)
    plt.ylabel(y_label)
    plt.title(title)
    
    # Rotate x-axis labels for better readability
    plt.xticks(rotation=45, ha='right')  # Rotate labels 45 degrees
    
    # Adjust layout to prevent label cutoff
    plt.tight_layout()
    
    img = io.BytesIO()
    plt.savefig(img, format='png', bbox_inches='tight')  # Add bbox_inches='tight' to prevent label cutoff
    img.seek(0)
    chart_url = base64.b64encode(img.getvalue()).decode()
    plt.close()
    return f"data:image/png;base64,{chart_url}"

# Add sync helper functions
def add_to_sync_queue(data_type, data):
    sync_entry = SyncQueue(
        data_type=data_type,
        data=data,
        synced=False
    )
    db.session.add(sync_entry)
    db.session.commit()

def get_aws_session():
    return boto3.Session(
        aws_access_key_id=app.config['AWS_ACCESS_KEY'],
        aws_secret_access_key=app.config['AWS_SECRET_KEY'],
        region_name=app.config['AWS_REGION']
    )

def get_cognito_token():
    client = get_aws_session().client('cognito-idp')
    try:
        response = client.admin_initiate_auth(
            UserPoolId=app.config['COGNITO_USER_POOL_ID'],
            ClientId=app.config['COGNITO_APP_CLIENT_ID'],
            AuthFlow='ADMIN_NO_SRP_AUTH',
            AuthParameters={
                'USERNAME': app.config['AWS_SYNC_USERNAME'],
                'PASSWORD': app.config['AWS_SYNC_PASSWORD']
            }
        )
        return response['AuthenticationResult']['IdToken']
    except ClientError as e:
        current_app.logger.error(f"Cognito authentication error: {str(e)}")
        return None

def start_sync_worker():
    def sync_worker():
        while True:
            try:
                with app.app_context():
                    # Get unsynced entries
                    unsynced = SyncQueue.query.filter_by(synced=False).all()
                    
                    # Get authentication token
                    token = get_cognito_token()
                    if not token:
                        raise Exception("Failed to get authentication token")

                    for entry in unsynced:
                        try:
                            # Create AWS API Gateway client
                            api_client = get_aws_session().client('apigatewaymanagementapi')
                            
                            # Make API request
                            response = api_client.post_to_connection(
                                Data=json.dumps({
                                    'type': entry.data_type,
                                    'data': entry.data
                                }).encode('utf-8'),
                                ConnectionId='your-api-endpoint'
                            )
                            
                            if response['ResponseMetadata']['HTTPStatusCode'] == 200:
                                entry.synced = True
                                db.session.commit()
                        except Exception as e:
                            current_app.logger.error(f"Sync error for entry {entry.id}: {str(e)}")
                            
            except Exception as e:
                current_app.logger.error(f"Sync worker error: {str(e)}")
                
            time.sleep(300)  # 5 minutes

    sync_thread = threading.Thread(target=sync_worker, daemon=True)
    sync_thread.start()

# Routes
@app.route('/', methods=['GET', 'POST'])
def home():
    # Get all available years and villages from the database
    years = Year.query.order_by(Year.year.desc()).all()
    villages = Village.query.order_by(Village.name).all()
    
    # Get selected filters
    year_filter = request.args.get('year', 'All')
    village_filter = request.args.get('village', 'All')

    # Query with filters
    patients_query = Patient.query.join(FolderSystem, Patient.id == FolderSystem.patient_id)
    if year_filter != 'All' and year_filter.isdigit():
        patients_query = patients_query.filter(FolderSystem.year == int(year_filter))
    if village_filter != 'All':
        patients_query = patients_query.filter(FolderSystem.village == village_filter)

    patients = patients_query.all()

    # Prepare data for table
    patients_data = []
    conditions_counter = Counter()
    prescriptions_counter = Counter()

    for patient in patients:
        conditions = [pc.condition.name for pc in PatientsConditions.query.filter_by(patient_id=patient.id).all()]
        prescriptions = [pp.prescription.name for pp in PatientsPrescriptions.query.filter_by(patient_id=patient.id).all()]

        patients_data.append({
            'id': patient.id,
            'name': f"{patient.first_name} {patient.last_name}",
            'age': patient.age,
            'gender': patient.gender,
            'blood_pressure': patient.blood_pressure,
            'conditions': conditions,
            'prescriptions': prescriptions
        })

        conditions_counter.update(conditions)
        prescriptions_counter.update(prescriptions)

    # Generate charts
    conditions_chart = generate_chart(conditions_counter, 'Conditions', 'Total', 'Condition Distribution')
    prescriptions_chart = generate_chart(prescriptions_counter, 'Prescriptions', 'Total', 'Prescription Distribution')

    return render_template('home.html', 
                         patients_data=patients_data,
                         conditions_chart=conditions_chart,
                         prescriptions_chart=prescriptions_chart,
                         years=years,
                         villages=villages,
                         year_filter=year_filter,
                         village_filter=village_filter)

@app.route('/create_ticket', methods=['GET', 'POST'])
def create_ticket():
    form = TicketForm()
    form.village.choices = [(v.name, v.name) for v in Village.query.order_by(Village.name).all()]
    form.year.choices = [(y.year, str(y.year)) for y in Year.query.order_by(Year.year.desc()).all()]
    form.conditions.choices = [(c.id, c.name) for c in Condition.query.all()]
    form.prescriptions.choices = [(p.id, p.name) for p in Prescription.query.all()]
    
    if form.validate_on_submit():
        try:
            # Create patient data dictionary
            patient_data = {
                'first_name': form.first_name.data,
                'last_name': form.last_name.data,
                'age': form.age.data,
                'gender': form.gender.data,
                'blood_pressure': form.blood_pressure.data,
                'conditions': form.conditions.data,
                'prescriptions': form.prescriptions.data,
                'year': form.year.data,
                'village': form.village.data,
                'reason_for_diff_medication': form.reason_for_diff_medication.data
            }

            # Always save to local database
            new_patient = Patient(
                first_name=form.first_name.data,
                last_name=form.last_name.data,
                age=form.age.data,
                gender=form.gender.data,
                blood_pressure=form.blood_pressure.data
            )
            db.session.add(new_patient)
            db.session.flush()

            for condition_id in form.conditions.data:
                db.session.add(PatientsConditions(patient_id=new_patient.id, condition_id=condition_id))
            
            for prescription_id in form.prescriptions.data:
                db.session.add(PatientsPrescriptions(
                    patient_id=new_patient.id,
                    prescription_id=prescription_id,
                    reason_for_diff_medication=form.reason_for_diff_medication.data
                ))
            
            folder_entry = FolderSystem(year=form.year.data, village=form.village.data, patient_id=new_patient.id)
            db.session.add(folder_entry)

            # Add to sync queue for later synchronization
            add_to_sync_queue('patient', patient_data)
            
            db.session.commit()
            return redirect(url_for('home'))
            
        except SQLAlchemyError as e:
            db.session.rollback()
            return render_template('create_ticket.html', form=form, 
                                error="Database error. Data saved for later sync.")

    return render_template('create_ticket.html', form=form)


@app.route('/add_condition', methods=['GET', 'POST'])
def add_condition():
    form = ConditionForm()
    if form.validate_on_submit():
        # Check if the condition already exists
        existing_condition = Condition.query.filter_by(name=form.name.data).first()
        if existing_condition:
            # Flash a message or handle duplicate condition error
            return render_template('add_condition.html', form=form, error="Condition already exists.")
        
        # Add the new condition if it doesn't exist
        new_condition = Condition(name=form.name.data)
        db.session.add(new_condition)
        db.session.commit()
        return redirect(url_for('home'))
    
    return render_template('add_condition.html', form=form)


@app.route('/add_prescription', methods=['GET', 'POST'])
def add_prescription():
    form = PrescriptionForm()
    if form.validate_on_submit():
        # Check if the prescription already exists
        existing_prescription = Prescription.query.filter_by(name=form.name.data).first()
        if existing_prescription:
            return render_template('add_prescription.html', form=form, error="Prescription already exists.")
        
        # Add the new prescription if it doesn't exist
        new_prescription = Prescription(
            name=form.name.data,
        )
        db.session.add(new_prescription)
        db.session.commit()
        return redirect(url_for('home'))
    
    return render_template('add_prescription.html', form=form)


@app.route('/delete_patient/<int:patient_id>')
def delete_patient(patient_id):
    patient = Patient.query.get(patient_id)
    if patient:
        PatientsConditions.query.filter_by(patient_id=patient_id).delete()
        PatientsPrescriptions.query.filter_by(patient_id=patient_id).delete()
        FolderSystem.query.filter_by(patient_id=patient_id).delete()
        db.session.delete(patient)
        db.session.commit()
    return redirect(url_for('home'))

@app.route('/sync_status')
def sync_status():
    unsynced_count = SyncQueue.query.filter_by(synced=False).count()
    last_synced = SyncQueue.query.filter_by(synced=True).order_by(SyncQueue.created_at.desc()).first()
    
    return render_template('sync_status.html',
                         unsynced_count=unsynced_count,
                         last_synced=last_synced.created_at if last_synced else None)

@app.route('/trigger_sync')
def trigger_sync():
    # Implement manual sync trigger
    # This could start an immediate sync attempt
    return redirect(url_for('sync_status'))

@app.route('/edit_patient/<int:patient_id>', methods=['GET', 'POST'])
def edit_patient(patient_id):
    patient = Patient.query.get_or_404(patient_id)
    folder = FolderSystem.query.filter_by(patient_id=patient_id).first()
    
    # Pre-fill the form with current patient data
    form = TicketForm()
    form.village.choices = [(v.name, v.name) for v in Village.query.order_by(Village.name).all()]
    form.year.choices = [(y.year, str(y.year)) for y in Year.query.order_by(Year.year.desc()).all()]
    form.conditions.choices = [(c.id, c.name) for c in Condition.query.all()]
    form.prescriptions.choices = [(p.id, p.name) for p in Prescription.query.all()]
    
    if request.method == 'GET':
        form.year.data = folder.year
        form.village.data = folder.village
        form.first_name.data = patient.first_name
        form.last_name.data = patient.last_name
        form.age.data = patient.age
        form.gender.data = patient.gender
        form.blood_pressure.data = patient.blood_pressure
        
        # Get current conditions and prescriptions
        current_conditions = [pc.condition_id for pc in PatientsConditions.query.filter_by(patient_id=patient_id).all()]
        current_prescriptions = [pp.prescription_id for pp in PatientsPrescriptions.query.filter_by(patient_id=patient_id).all()]
        
        form.conditions.data = current_conditions
        form.prescriptions.data = current_prescriptions
        
        # Get reason for different medication
        prescription_record = PatientsPrescriptions.query.filter_by(patient_id=patient_id).first()
        if prescription_record:
            form.reason_for_diff_medication.data = prescription_record.reason_for_diff_medication
    
    if form.validate_on_submit():
        try:
            # Update patient information
            patient.first_name = form.first_name.data
            patient.last_name = form.last_name.data
            patient.age = form.age.data
            patient.gender = form.gender.data
            patient.blood_pressure = form.blood_pressure.data
            
            # Update folder information
            folder.year = form.year.data
            folder.village = form.village.data
            
            # Update conditions
            PatientsConditions.query.filter_by(patient_id=patient_id).delete()
            for condition_id in form.conditions.data:
                db.session.add(PatientsConditions(patient_id=patient_id, condition_id=condition_id))
            
            # Update prescriptions
            PatientsPrescriptions.query.filter_by(patient_id=patient_id).delete()
            for prescription_id in form.prescriptions.data:
                db.session.add(PatientsPrescriptions(
                    patient_id=patient_id,
                    prescription_id=prescription_id,
                    reason_for_diff_medication=form.reason_for_diff_medication.data
                ))
            
            db.session.commit()
            return redirect(url_for('home'))
            
        except SQLAlchemyError as e:
            db.session.rollback()
            return render_template('edit_patient.html', form=form, patient=patient,
                                error="Database error. Please try again.")
    
    return render_template('edit_patient.html', form=form, patient=patient)

@app.route('/add_locations', methods=['GET', 'POST'])
def add_locations():
    village_form = VillageForm()
    year_form = YearForm()
    
    if village_form.submit.data and village_form.validate():
        existing_village = Village.query.filter_by(name=village_form.name.data).first()
        if not existing_village:
            new_village = Village(name=village_form.name.data)
            db.session.add(new_village)
            db.session.commit()
            return redirect(url_for('add_locations'))
    
    if year_form.submit.data and year_form.validate():
        existing_year = Year.query.filter_by(year=year_form.year.data).first()
        if not existing_year:
            new_year = Year(year=year_form.year.data)
            db.session.add(new_year)
            db.session.commit()
            return redirect(url_for('add_locations'))
    
    villages = Village.query.order_by(Village.name).all()
    years = Year.query.order_by(Year.year.desc()).all()
    
    return render_template('add_locations.html', 
                         village_form=village_form,
                         year_form=year_form,
                         villages=villages,
                         years=years)


@app.route('/manage_conditions', methods=['GET', 'POST'])
def manage_conditions():
    form = ConditionForm()
    if form.validate_on_submit():
        existing_condition = Condition.query.filter_by(name=form.name.data).first()
        if not existing_condition:
            new_condition = Condition(name=form.name.data)
            db.session.add(new_condition)
            db.session.commit()
            return redirect(url_for('manage_conditions'))
        else:
            return render_template('manage_conditions.html', form=form, 
                                conditions=Condition.query.order_by(Condition.name).all(),
                                error="Condition already exists")
    
    return render_template('manage_conditions.html', form=form, 
                         conditions=Condition.query.order_by(Condition.name).all())

@app.route('/manage_prescriptions', methods=['GET', 'POST'])
def manage_prescriptions():
    form = PrescriptionForm()
    if form.validate_on_submit():
        existing_prescription = Prescription.query.filter_by(name=form.name.data).first()
        if not existing_prescription:
            new_prescription = Prescription(name=form.name.data)
            db.session.add(new_prescription)
            db.session.commit()
            return redirect(url_for('manage_prescriptions'))
        else:
            return render_template('manage_prescriptions.html', form=form, 
                                prescriptions=Prescription.query.order_by(Prescription.name).all(),
                                error="Prescription already exists")
    
    return render_template('manage_prescriptions.html', form=form, 
                         prescriptions=Prescription.query.order_by(Prescription.name).all())

@app.route('/manage_villages', methods=['GET', 'POST'])
def manage_villages():
    form = VillageForm()
    if form.validate_on_submit():
        existing_village = Village.query.filter_by(name=form.name.data).first()
        if not existing_village:
            new_village = Village(name=form.name.data)
            db.session.add(new_village)
            db.session.commit()
            return redirect(url_for('manage_villages'))
        else:
            return render_template('manage_villages.html', form=form, 
                                villages=Village.query.order_by(Village.name).all(),
                                error="Village already exists")
    
    return render_template('manage_villages.html', form=form, 
                         villages=Village.query.order_by(Village.name).all())

@app.route('/manage_years', methods=['GET', 'POST'])
def manage_years():
    form = YearForm()
    if form.validate_on_submit():
        existing_year = Year.query.filter_by(year=form.year.data).first()
        if not existing_year:
            new_year = Year(year=form.year.data)
            db.session.add(new_year)
            db.session.commit()
            return redirect(url_for('manage_years'))
        else:
            return render_template('manage_years.html', form=form, 
                                years=Year.query.order_by(Year.year.desc()).all(),
                                error="Year already exists")
    
    return render_template('manage_years.html', form=form, 
                         years=Year.query.order_by(Year.year.desc()).all())

@app.route('/edit_condition/<int:id>', methods=['GET', 'POST'])
def edit_condition(id):
    condition = Condition.query.get_or_404(id)
    form = ConditionForm(obj=condition)
    
    if form.validate_on_submit():
        existing = Condition.query.filter(Condition.name == form.name.data, Condition.id != id).first()
        if existing:
            return render_template('edit_generic.html', form=form, 
                                title="Condition", item=condition,
                                error="A condition with this name already exists")
        condition.name = form.name.data
        db.session.commit()
        return redirect(url_for('manage_conditions'))
    
    return render_template('edit_generic.html', form=form, title="Condition", item=condition)

@app.route('/edit_prescription/<int:id>', methods=['GET', 'POST'])
def edit_prescription(id):
    prescription = Prescription.query.get_or_404(id)
    form = PrescriptionForm(obj=prescription)
    
    if form.validate_on_submit():
        existing = Prescription.query.filter(Prescription.name == form.name.data, Prescription.id != id).first()
        if existing:
            return render_template('edit_generic.html', form=form, 
                                title="Prescription", item=prescription,
                                error="A prescription with this name already exists")
        prescription.name = form.name.data
        db.session.commit()
        return redirect(url_for('manage_prescriptions'))
    
    return render_template('edit_generic.html', form=form, title="Prescription", item=prescription)

@app.route('/edit_village/<int:id>', methods=['GET', 'POST'])
def edit_village(id):
    village = Village.query.get_or_404(id)
    form = VillageForm(obj=village)
    
    if form.validate_on_submit():
        existing = Village.query.filter(Village.name == form.name.data, Village.id != id).first()
        if existing:
            return render_template('edit_generic.html', form=form, 
                                title="Village", item=village,
                                error="A village with this name already exists")
        village.name = form.name.data
        db.session.commit()
        return redirect(url_for('manage_villages'))
    
    return render_template('edit_generic.html', form=form, title="Village", item=village)

@app.route('/edit_year/<int:id>', methods=['GET', 'POST'])
def edit_year(id):
    year = Year.query.get_or_404(id)
    form = YearForm(obj=year)
    
    if form.validate_on_submit():
        existing = Year.query.filter(Year.year == form.year.data, Year.id != id).first()
        if existing:
            return render_template('edit_generic.html', form=form, 
                                title="Year", item=year,
                                error="This year already exists")
        year.year = form.year.data
        db.session.commit()
        return redirect(url_for('manage_years'))
    
    return render_template('edit_generic.html', form=form, title="Year", item=year)

@app.route('/delete_condition/<int:id>')
def delete_condition(id):
    condition = Condition.query.get_or_404(id)
    try:
        db.session.delete(condition)
        db.session.commit()
    except:
        db.session.rollback()
    return redirect(url_for('manage_conditions'))

@app.route('/delete_prescription/<int:id>')
def delete_prescription(id):
    prescription = Prescription.query.get_or_404(id)
    try:
        db.session.delete(prescription)
        db.session.commit()
    except:
        db.session.rollback()
    return redirect(url_for('manage_prescriptions'))

@app.route('/delete_village/<int:id>')
def delete_village(id):
    village = Village.query.get_or_404(id)
    try:
        db.session.delete(village)
        db.session.commit()
    except:
        db.session.rollback()
    return redirect(url_for('manage_villages'))

@app.route('/delete_year/<int:id>')
def delete_year(id):
    year = Year.query.get_or_404(id)
    try:
        db.session.delete(year)
        db.session.commit()
    except:
        db.session.rollback()
    return redirect(url_for('manage_years'))

class MedicalApiStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Create DynamoDB tables
        patients_table = dynamodb.Table(
            self, 'PatientsTable',
            partition_key=dynamodb.Attribute(
                name='id',
                type=dynamodb.AttributeType.STRING
            )
        )

        # Create Cognito User Pool
        user_pool = cognito.UserPool(
            self, 'MedicalUserPool',
            user_pool_name='medical-user-pool',
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True)
        )

        # Create Lambda functions
        get_patients_lambda = lambda_.Function(
            self, 'GetPatientsFunction',
            runtime=lambda_.Runtime.PYTHON_3_9,
            handler='patients.handler',
            code=lambda_.Code.from_asset('lambda')
        )

        # Grant Lambda functions access to DynamoDB
        patients_table.grant_read_write_data(get_patients_lambda)

        # Create API Gateway
        api = apigateway.RestApi(
            self, 'MedicalApi',
            rest_api_name='Medical API',
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=apigateway.Cors.ALL_METHODS
            )
        )

        # Add Cognito Authorizer
        auth = apigateway.CognitoUserPoolsAuthorizer(
            self, 'MedicalApiAuthorizer',
            cognito_user_pools=[user_pool]
        )

        # Create API endpoints
        patients = api.root.add_resource('patients')
        patients.add_method(
            'GET',
            apigateway.LambdaIntegration(get_patients_lambda),
            authorizer=auth,
            authorization_type=apigateway.AuthorizationType.COGNITO
        )

def handler(event, context):
    try:
        # Get all patients
        response = table.scan()
        patients = response['Items']
        
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Credentials': True
            },
            'body': json.dumps({
                'patients': patients
            })
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e)
            })
        }

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    start_sync_worker()
    app.run(debug=True)




