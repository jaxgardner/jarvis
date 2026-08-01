import SwiftUI
import UIKit

/// The camera, wrapped for SwiftUI, handing back a photo already sized for
/// the vision API.
///
/// `UIImagePickerController` rather than a custom `AVCaptureSession`: the
/// stock camera already has the tap-to-focus and exposure controls that make
/// a legible photo of a crumpled receipt, and reimplementing them badly would
/// cost extraction accuracy for no gain.
struct ReceiptCamera: UIViewControllerRepresentable {
    let onCapture: (Data) -> Void
    @Environment(\.dismiss) private var dismiss

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        // The simulator has no camera; falling back keeps the flow testable.
        picker.sourceType =
            UIImagePickerController.isSourceTypeAvailable(.camera) ? .camera : .photoLibrary
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ picker: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(onCapture: onCapture, dismiss: { dismiss() })
    }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate,
        UINavigationControllerDelegate
    {
        private let onCapture: (Data) -> Void
        private let dismiss: () -> Void

        init(onCapture: @escaping (Data) -> Void, dismiss: @escaping () -> Void) {
            self.onCapture = onCapture
            self.dismiss = dismiss
        }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            defer { dismiss() }
            guard let image = info[.originalImage] as? UIImage,
                let jpeg = image.receiptJPEG()
            else { return }
            onCapture(jpeg)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            dismiss()
        }
    }
}

extension UIImage {
    /// Downscaled JPEG, ready to upload.
    ///
    /// 1568px on the long edge is the point past which Anthropic's vision API
    /// gains nothing — a larger image is more tokens, a slower upload and the
    /// same extraction. A full 12MP photo is roughly 4MB; this is about 300KB.
    ///
    /// Also normalizes orientation, which matters more than it sounds: a
    /// receipt photographed in portrait carries an EXIF rotation flag that
    /// re-drawing bakes in. Without this the model reads a sideways receipt.
    func receiptJPEG(maxEdge: CGFloat = 1568, quality: CGFloat = 0.8) -> Data? {
        let longest = max(size.width, size.height)
        let scale = longest > maxEdge ? maxEdge / longest : 1
        let target = CGSize(width: size.width * scale, height: size.height * scale)

        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: target, format: format)
        let flattened = renderer.image { _ in
            draw(in: CGRect(origin: .zero, size: target))
        }
        return flattened.jpegData(compressionQuality: quality)
    }
}
