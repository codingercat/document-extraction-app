# app.py - Production version
import os
import uuid
import zipfile
from PIL import Image
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify
from werkzeug.utils import secure_filename
from landingai.predict import Predictor
from landingai.postprocess import crop
import logging
from pdf2image import convert_from_path
import gunicorn  # For production deployment
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}) 


secret_key = os.environ.get('SECRET_KEY')
if not secret_key:
    logger.warning("No SECRET_KEY set in environment! Using a random key.")
    secret_key = os.urandom(24).hex()
app.config['SECRET_KEY'] = secret_key

app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'input_images')
app.config['OUTPUT_FOLDER'] = os.environ.get('OUTPUT_FOLDER', 'output_images')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_SIZE', 50 * 1024 * 1024))  # Default 50MB
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'jpg', 'jpeg', 'png', 'gif', 'bmp'}

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
os.makedirs('tmp', exist_ok=True)



# Define API credentials for LandingAI - use environment variables
endpoint_id = os.environ.get('LANDINGAI_ENDPOINT_ID')  # Remove the default value
api_key = os.environ.get('LANDINGAI_API_KEY')  # Remove the default value

# Verify credentials are available
if not endpoint_id or not api_key:
    logger.error("Missing LandingAI credentials. Set LANDINGAI_ENDPOINT_ID and LANDINGAI_API_KEY environment variables.")
    predictor = None
else:
    try:
        predictor = Predictor(endpoint_id, api_key=api_key)
        logger.info("LandingAI predictor initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing LandingAI predictor: {str(e)}")
        predictor = None


try:
    predictor = Predictor(endpoint_id, api_key=api_key)
    logger.info("LandingAI predictor initialized successfully")
except Exception as e:
    logger.error(f"Error initializing LandingAI predictor: {str(e)}")
    predictor = None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    logger.info(f"Upload request received. Files in request: {list(request.files.keys())}")
    # ==== DEBUG SCRIPT ====
    logger.info(f"Request Content-Type: {request.content_type}")
    logger.info(f"Request Form Fields: {request.form.to_dict()}")
    logger.info(f"Request Files Fields: { {k: v.filename for k, v in request.files.items()} }")
    # ==== END DEBUG ====

    if 'file' not in request.files:
        logger.error("No 'file' field in request.files")
        return jsonify(error="No file part"), 400  # ← changed from redirect to JSON
    
    # Wrap single file into a list
    files = [request.files['file']]
    logger.info(f"Number of files received: {len(files)}")
    
    if not files or files[0].filename == '':
        logger.error("Empty files list or no filename")
        return jsonify(error="No selected file"), 400  # ← changed from redirect to JSON
    
    # Create a unique session ID for this batch
    session_id = str(uuid.uuid4())
    upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    output_folder = os.path.join(app.config['OUTPUT_FOLDER'], session_id)
    
    os.makedirs(upload_folder, exist_ok=True)
    os.makedirs(output_folder, exist_ok=True)
    
    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_path = os.path.join(upload_folder, filename)
            file.save(file_path)
            logger.info(f"Saved uploaded file: {file_path}")
            
            # For PDFs, convert to images first
            if filename.lower().endswith('.pdf'):
                pdf_to_images(file_path, upload_folder)
    
    # Process all images in the upload folder
    try:
        process_images(upload_folder, output_folder)
        logger.info(f"Processing completed for session {session_id}")
    except Exception as e:
        logger.error(f"Error during processing: {str(e)}")
        flash(f"Error during image processing: {str(e)}")
        return redirect(url_for('index'))
    
    # Create ZIP file with all processed images
    zip_path = create_zip(output_folder, session_id)
    logger.info(f"Created zip file: {zip_path}")
    
    # Clean up the input folder to save space
    try:
        import shutil
        shutil.rmtree(upload_folder)
        logger.info(f"Cleaned up input folder: {upload_folder}")
    except Exception as e:
        logger.warning(f"Could not clean up input folder: {str(e)}")
    
    return redirect(url_for('download_file', filename=os.path.basename(zip_path), session_id=session_id))

def pdf_to_images(pdf_path, output_dir):
    """Convert PDF to images (page by page) for processing"""
    try:
        logger.info(f"Converting PDF to images: {pdf_path}")
        base_filename = os.path.splitext(os.path.basename(pdf_path))[0]

        # Get the number of pages
        from pdf2image import pdfinfo_from_path
        info = pdfinfo_from_path(pdf_path)
        total_pages = info.get("Pages", 0)
        logger.info(f"{pdf_path} has {total_pages} pages")

        for page_num in range(1, total_pages + 1):
            images = convert_from_path(pdf_path, dpi=150, first_page=page_num, last_page=page_num)
            image_path = os.path.join(output_dir, f"{base_filename}_page_{page_num}.png")
            images[0].save(image_path, "PNG")
            logger.info(f"Saved page {page_num} as image: {image_path}")

        # Remove the original PDF file after processing
        os.remove(pdf_path)
    except Exception as e:
        logger.error(f"Error converting PDF to images: {str(e)}")
        flash(f"Error processing PDF: {str(e)}")

def process_images(input_dir, output_dir):
    """Process all images in the input directory using the LandingAI model"""
    if predictor is None:
        raise Exception("Image processing service is not available")
        
    for filename in os.listdir(input_dir):
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")):
            file_path = os.path.join(input_dir, filename)
            
            logger.info(f"Processing: {file_path}")
            
            try:
                # Open and process the image
                image = Image.open(file_path)
                predictions = predictor.predict(image)
                output_images = crop(predictions, image)
                
                # Save processed images
                image_name = os.path.splitext(filename)[0]
                for i, img in enumerate(output_images):
                    output_filename = f"{image_name}_crop_{i+1}.png"
                    output_path = os.path.join(output_dir, output_filename)
                    img.save(output_path)
                    logger.info(f"Saved: {output_path}")
                
                # If no crops were made, save the original image
                if len(output_images) == 0:
                    logger.warning(f"No objects detected in {filename}, saving original")
                    output_filename = f"{image_name}_original.png"
                    output_path = os.path.join(output_dir, output_filename)
                    image.save(output_path)
                    logger.info(f"Saved original: {output_path}")
            except Exception as e:
                logger.error(f"Error processing image {filename}: {str(e)}")
                # Save the original image in case of error
                try:
                    image_name = os.path.splitext(filename)[0]
                    output_filename = f"{image_name}_error.png"
                    output_path = os.path.join(output_dir, output_filename)
                    image = Image.open(file_path)
                    image.save(output_path)
                    logger.info(f"Saved original due to error: {output_path}")
                except:
                    logger.error(f"Could not save original image for {filename}")

def create_zip(folder_path, session_id):
    """Create a ZIP file from the folder"""
    zip_filename = f"{session_id}_extracted_images.zip"
    zip_path = os.path.join('tmp', zip_filename)
    
    files_in_folder = os.listdir(folder_path)
    logger.info(f"Creating zip from {folder_path} with {len(files_in_folder)} files")
    
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, folder_path)
                logger.info(f"Adding to zip: {file_path} as {arcname}")
                zipf.write(file_path, arcname)
    
    return zip_path

@app.route('/download/<filename>')
def download_file(filename):
    session_id = request.args.get('session_id')
    
    response = send_file(os.path.join('tmp', filename), 
                     as_attachment=True,
                     download_name='extracted_images.zip')
    
    # Schedule cleanup after response is sent
    @response.call_on_close
    def cleanup():
        try:
            # Remove the zip file and output folder after download
            os.remove(os.path.join('tmp', filename))
            import shutil
            output_folder = os.path.join(app.config['OUTPUT_FOLDER'], session_id)
            if os.path.exists(output_folder):
                shutil.rmtree(output_folder)
            logger.info(f"Cleaned up after download: {filename} and {output_folder}")
        except Exception as e:
            logger.warning(f"Cleanup error: {str(e)}")
    
    return response

# For wsgi servers
application = app

if __name__ == '__main__':
    # Only for development
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))