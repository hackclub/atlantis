import Frame from './shared/Frame'

const Projects = ({ onClose }: { onClose: () => void }) => {
  return (
    <div className="fixed top-0 left-0 w-full h-full bg-backdrop z-10 overflow-hidden" onClick={onClose}>
      <div className="w-full h-full flex flex-col items-center justify-end translate-y-30">
        <h1 className="text-light-brown">PROJECTS</h1>
        <div onClick={(e) => e.stopPropagation()}>
          <Frame>
            <p>projek</p>
          </Frame>
        </div>
      </div>
    </div>
  )
}

export default Projects
