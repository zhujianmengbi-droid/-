using System.Drawing;
using System.Windows.Forms;

namespace FlashbackRecorder;

// Keep disabled actions readable on the dark window, including their purpose.
internal sealed class RecorderButton : Button
{
    string description = "";
    bool hovering;
    public string Description { get => description; set { description = value; AccessibleDescription = value; Invalidate(); } }
    public RecorderButton()
    {
        FlatStyle = FlatStyle.Flat;
        UseVisualStyleBackColor = false;
        BackColor = Color.FromArgb(32, 37, 44);
        ForeColor = Color.FromArgb(188, 193, 202);
        FlatAppearance.BorderColor = Color.FromArgb(66, 73, 84);
        Margin = new Padding(4);
        SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
    }
    protected override void OnMouseEnter(EventArgs e) { hovering = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hovering = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnPaint(PaintEventArgs e)
    {
        var scale = DeviceDpi / 96f;
        e.Graphics.Clear(Enabled && hovering ? Color.FromArgb(42, 48, 57) : BackColor);
        using var pen = new Pen(Enabled ? Color.FromArgb(66, 73, 84) : Color.FromArgb(47, 52, 60));
        e.Graphics.DrawRectangle(pen, 0, 0, Math.Max(0, Width - 1), Math.Max(0, Height - 1));
        var titleColour = Enabled ? ForeColor : Color.FromArgb(125, 132, 142);
        var flags = TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis;
        var bounds = Rectangle.Inflate(ClientRectangle, -(int)(6 * scale), -(int)(3 * scale));
        if (Description.Length == 0 || Height < 38 * scale)
            TextRenderer.DrawText(e.Graphics, Text, Font, bounds, titleColour, flags);
        else
        {
            var middle = bounds.Top + bounds.Height / 2;
            TextRenderer.DrawText(e.Graphics, Text, Font, new Rectangle(bounds.X, bounds.Y, bounds.Width, bounds.Height / 2), titleColour, flags);
            using var small = new Font(Font.FontFamily, Math.Max(7.5f, Font.Size - 1), FontStyle.Regular);
            TextRenderer.DrawText(e.Graphics, Description, small, new Rectangle(bounds.X, middle, bounds.Width, bounds.Bottom - middle),
                Color.FromArgb(139, 148, 160), flags);
        }
        if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -4, -4), ForeColor, BackColor);
    }
}
