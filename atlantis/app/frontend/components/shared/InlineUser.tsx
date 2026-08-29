const InlineUser = ({ avatar, display_name }: { avatar: string; display_name: string }) => (
  <span>
    <img
      src={avatar}
      alt=""
      className="w-6 h-6 rounded-full inline align-middle mr-1.5 relative -top-px bg-brown border border-brown"
    />
    <span>{display_name}</span>
  </span>
)

export default InlineUser
