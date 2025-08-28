from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
import os

out_path = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "synthetic_bulletin.pdf")
os.makedirs(os.path.dirname(out_path), exist_ok=True)


c = canvas.Canvas(out_path, pagesize=LETTER)
w, h = LETTER

c.setFont("Helvetica-Bold", 14)
c.drawString(1*inch, h-1*inch, "2025 Retail Programs - August")
c.setFont("Helvetica", 11)
c.drawString(1*inch, h-1.4*inch, "Model: 2025 Super Sedan")
c.drawString(1*inch, h-1.7*inch, "Customer Cash: $1,500")
c.drawString(1*inch, h-1.95*inch, "Bonus Cash $500")
c.drawString(1*inch, 2.2*inch, "Owner Loyalty: $750")
c.drawString(1*inch, h-2.45*inch, "Conquest Cash - $1,000")
c.drawString(1*inch, h-2.9*inch, "APR as low as 1.9 for 36 months (not MONEY)")
c.drawString(1*inch, h-3.3*inch, "Lease Cash: 2,250 on approved credit")

c.showPage()
c.save()
print(f"Created {out_path}")
